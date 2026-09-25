"""Explicit local ownership commands and remote, read-only human inspection."""

import argparse
from contextlib import closing, contextmanager, ExitStack
import json
import math
import os
import re
import sys
from uuid import uuid4

from .authority import AuthorityError, JournalAuthority, TaskAuthority
from .client import TaskClientError, TaskServiceClient
from .config import ConfigError, create_service_token, load_config
from .execution import ExecutionError
from .discovery import DiscoveryError, connect
from .launcher import start, startup_election, stop
from .platform_support import PlatformError
from .inspection import run_inspection
from .journal import HeadStore, JournalError, JsonStore, PendingStore
from .leases import ClockError, EffectiveClock
from .maintenance import LeaseMaintenance
from .palace import PalaceClient, PalaceError
from .projection import ProjectionError, TaskProjector
from .server import ProjectionPump, serve


def parse_duration(value):
    if type(value) is not str or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)[smh]", value) is None:
        raise argparse.ArgumentTypeError("Duration must be a positive finite number with s/m/h suffix")
    try:
        seconds = float(value[:-1]) * {"s": 1, "m": 60, "h": 3600}[value[-1]]
    except (ValueError, OverflowError):
        seconds = float("inf")
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Duration must be positive and finite")
    return seconds


def _unstarted_clock():
    raise ClockError("clock_not_owned", "Effective clock requires authority ownership")


def _palace(config):
    return PalaceClient(config.hub_url, f"mptask/{config.authority_id}", token=config.hub_token)


def _require_empty_initial_stream(client):
    """Necessary for a new clock, not proof that a reused identity has no lost in-flight write."""
    descriptor = client.discover()
    if type(descriptor) is not dict or descriptor.get("profile") not in (
            "mempalace-ordered-v1", "mempalace-legacy-append-order-v1"):
        raise PalaceError("unsupported_profile", "Cannot verify an empty stream without a supported profile")
    absent = object()
    with closing(client.replay_events()) as records:
        if next(records, absent) is not absent:
            raise ClockError("missing_clock_state", "Cannot initialize a missing clock over remote history")


def _check_configuration(authority, config, *, allow_empty=False):
    configuration = authority.state.configuration
    if configuration is None:
        if allow_empty:
            return
        raise AuthorityError("not_initialized", "Run explicit init before starting this authority")
    if configuration != config.configuration:
        raise AuthorityError("configuration_conflict", "Requested genesis differs from accepted configuration")


@contextmanager
def owned_authority(config, *, initialize=False):
    """Exclusive local owner; all effective-clock construction follows lock acquisition."""
    if config.recovery_mode == "journal":
        authority = JournalAuthority(
            config.authority_id, _palace(config), config.runtime_dir,
            expected_configuration=config.configuration, initialize=initialize,
            system_actor=config.maintenance_actor, recovery_actor=config.recovery_actor,
        )
        try:
            authority.start()
            yield authority
        finally:
            authority.close()
        return
    if not initialize and not config.state_dir.is_dir():
        raise AuthorityError("not_initialized", "Run explicit init before starting this authority")
    clock_store = JsonStore(config.state_dir / "clock.json")
    client = _palace(config)

    def clock_factory():
        if initialize and not clock_store.path.exists():
            if HeadStore(config.state_dir).read() is not None or PendingStore(config.state_dir).read() is not None:
                raise ClockError("missing_clock_state", "Cannot replace a lost clock over existing recovery state")
            _require_empty_initial_stream(client)
        return EffectiveClock(clock_store, initialize=initialize)

    authority = TaskAuthority(config.authority_id, client, config.state_dir,
                              clock=_unstarted_clock)
    try:
        authority.start(clock_factory=clock_factory)
        _check_configuration(authority, config, allow_empty=initialize)
        yield authority
    finally:
        authority.close()


def initialize(config, *, generate_token=False):
    """Explicit genesis creation or verification, not an implicit serve fallback.

    Journal mode reconstructs existing state from the palace alone. Legacy mode
    still requires its matching local recovery files and effective clock.
    """
    with owned_authority(config, initialize=True) as authority:
        if config.service_token is None:
            if not generate_token:
                raise ConfigError("Missing service credential; explicit --generate-token is required")
            create_service_token(config.service_token_file)
        if authority.state.configuration is not None:
            return {"ok": True, "already_initialized": True, "authority_id": config.authority_id}
        with authority.serialized():
            if config.recovery_mode == "legacy" and authority.clock_source.pending_reboot:
                authority.clock_source.acknowledge_reboot(active_attempts=0)
        # Journal init is scoped to this activation; legacy retains persisted pending.
        execute = (authority.execute_current if config.recovery_mode == "journal"
                   else authority.execute)
        return execute({
            **config.genesis, "operation": "authority_create", "command_id": str(uuid4()),
        })


def _maintenance(authority, config):
    port = authority.bound_current() if config.recovery_mode == "journal" else authority
    return LeaseMaintenance(port, authority.clock_source,
                            system_actor=config.maintenance_actor, recovery_actor=config.recovery_actor,
                            batch_limit=100)


def _projector(config):
    if config.recovery_mode == "journal":
        raise ConfigError("Journal-mode projection requires a disposable epoch-aware integration",
                          code="projection_unsupported")
    return TaskProjector(_palace(config), JsonStore(config.state_dir / "projection.json"),
                         config.project_wings)


def supervise(config, *, profiles, supervisor_id, worker_id, max_steps,
              interval_seconds=1, pool_size=1, filters=None, stop_event=None):
    """Explicit bounded host integration, NOT native Copilot/fleet dispatch.

    Profiles are trusted, preconstructed LocalProfile instances, never task-provided
    argv or dynamically imported adapters. This mode excludes a simultaneous serve
    owner. Simultaneous MCP/worker orchestration is deliberately not provided;
    an external coordinator would need the same authority and serialized tick
    ownership rather than two independent maintenance drivers.
    """
    from .supervisor import HostSupervisor

    if (type(max_steps) is not int or not 1 <= max_steps <= 1000000
            or type(interval_seconds) not in (int, float)
            or not 0 <= interval_seconds <= 15):
        raise ConfigError("Supervision requires bounded max_steps and an interval in 0..15 seconds")
    with owned_authority(config) as authority:
        host = HostSupervisor(
            authority.bound_current() if config.recovery_mode == "journal" else authority,
            authority.clock_source, _maintenance(authority, config),
            supervisor_id=supervisor_id, worker_id=worker_id, profiles=profiles,
            pool_size=pool_size, filters=filters,
        )
        return host.run(max_steps=max_steps, interval_seconds=interval_seconds, stop_event=stop_event)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ConfigError("Invalid command-line arguments; use --help")


def _parser():
    parser = _Parser(prog="mempalace-tasks",
                     description="Explicit task service ownership and remote read-only inspection")
    parser.add_argument("--config", help="Absolute version-1 legacy or version-2 JSON configuration (or MPTASK_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("init", "Explicit new-authority init, not data-loss recovery; idempotent with intact state"),
        ("serve", "Run foreground authenticated MCP; requires prior init"),
        ("start", "Explicitly start/reuse a journal-mode launcher owner"),
        ("connect", "Discover an authenticated ready owner; never starts unless --start"),
        ("stop", "Drain the expected instance and confirm listener and ownership release"),
        ("inspect", "Query the running service's health, without local ownership"),
        ("reconcile", "Exclusive offline reconciliation/maintenance; service must be stopped"),
        ("project", "Exclusive offline historical projection; service must be stopped"),
        ("status", "Read a coherent task status snapshot"),
        ("list", "Read a coherent task snapshot page"),
        ("show", "Read one task and its authority-provided eligibility"),
        ("history", "Read task event history"),
        ("watch", "Watch bounded read-only snapshots"),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--config", default=argparse.SUPPRESS)
        if name in {"start", "connect", "stop"}:
            command.add_argument("--timeout", type=parse_duration, default=10.0)
        if name == "connect":
            command.add_argument("--start", action="store_true",
                                 help="Explicitly consent to configured launcher startup")
        if name == "stop":
            command.add_argument("--instance-id", required=True,
                                 help="Expected instance_id from a prior connection")
        if name == "serve":
            command.add_argument("--startup-ticket-stdin", action="store_true", help=argparse.SUPPRESS)
        if name == "init":
            command.epilog = (
                "Schema 2 replays the palace journal without matching local recovery files. "
                "Serve requires accepted genesis; only explicit init may create it. "
                "Schema 1 is legacy migration mode: missing recovery files are not permission "
                "to reuse an authority UUID; restore trusted state instead."
            )
            command.add_argument("--generate-token", action="store_true",
                                 help="Exclusively create a missing 0600 service token; never prints it")
        if name == "project":
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--resume", action="store_true")
            mode.add_argument("--rebuild", action="store_true",
                              help="Reset only the derived checkpoint, not remote history")
            command.add_argument("--max-batches", type=int, default=1)
            command.add_argument("--batch-size", type=int, default=10)
        if name in {"inspect", "status", "list", "show", "history", "watch"}:
            command.add_argument("--timeout", type=parse_duration, default=10.0,
                                 help="Overall timeout for each read, e.g. 10s")
        if name in {"status", "list", "show", "history", "watch"}:
            command.add_argument("--json", action="store_true", help="Canonical JSON, one frame per line")
            if name in {"show", "history"}:
                command.add_argument("task_id")
            if name in {"status", "list", "watch"}:
                command.add_argument("--project")
                command.add_argument("--goal", dest="goal_id")
                command.add_argument("--status")
                command.add_argument("--assignee")
                command.add_argument("--kind")
                command.add_argument("--needs-attention", action=argparse.BooleanOptionalAction, default=None)
            if name != "show":
                command.add_argument("--limit", type=int, default=100)
                command.add_argument("--cursor")
            if name == "history":
                command.add_argument("--after-record-seq", type=int, default=0)
            if name == "watch":
                command.add_argument("--refresh", dest="refresh_seconds", type=parse_duration, default=5.0)
                command.add_argument("--duration", dest="duration_seconds", type=parse_duration, default=600.0)
    return parser


def _emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def main(argv=None) -> int:
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as exit:
            return int(exit.code)
        if args.command == "project" and (
                not 1 <= args.max_batches <= 1000 or not 1 <= args.batch_size <= 100):
            raise ConfigError("Projection requires 1..1000 batches of 1..100 events")
        config = load_config(args.config, allow_missing_service_token=(
            args.command == "init" and args.generate_token))
        if config.recovery_mode == "journal" and (
                args.command == "project" or args.command == "serve" and config.projections_enabled):
            _projector(config)
        if args.command in {"start", "connect", "stop"}:
            if args.command == "stop":
                _emit(stop(config, expected_instance_id=args.instance_id, timeout=args.timeout))
            else:
                info = (start(args.config or os.environ.get("MPTASK_CONFIG"), timeout=args.timeout)
                        if args.command == "start" or args.start
                        else connect(config, timeout=args.timeout))
                _emit(info.public())
            return 0
        if args.command in {"inspect", "status", "list", "show", "history", "watch"}:
            info = connect(config, timeout=args.timeout) if config.recovery_mode == "journal" else None
            client = TaskServiceClient(
                info.url if info else config.service_url,
                token=info.token if info else config.service_token, timeout=args.timeout)
            if args.command == "inspect":
                health = client.call_tool("mptask_health", {})
                _emit(health)
                return 0 if (health.get("fresh") is True
                             and health.get("maintenance", {}).get("ok") is True
                             and not health.get("projection", {}).get("paused", False)) else 1
            options = {key: value for key, value in vars(args).items()
                       if key not in {"command", "config", "timeout"}}
            return run_inspection(client, args.command, options, stdout=sys.stdout, stderr=sys.stderr)
        if args.command == "init":
            result = initialize(config, generate_token=args.generate_token)
            _emit(result)
            return 0 if result["ok"] else 1
        if args.command == "serve" and (config.recovery_mode == "journal" or args.startup_ticket_stdin):
            ticket = None
            if args.startup_ticket_stdin:
                ticket = sys.stdin.buffer.read(2049)
                sys.stdin.close()
            with ExitStack() as election:
                election.enter_context(startup_election(config, ticket=ticket))
                with owned_authority(config) as authority:
                    serve(authority, _maintenance(authority, config), token=config.service_token,
                          host=config.host, port=config.port, config=config, on_ready=election.close)
            return 0
        with owned_authority(config) as authority:
            if args.command == "serve":
                serve(authority, _maintenance(authority, config), token=config.service_token,
                      host=config.host, port=config.port,
                      projector=_projector(config) if config.projections_enabled else None)
                return 0
            if args.command == "reconcile":
                maintenance = _maintenance(authority, config)
                tick = maintenance.tick()
                health = authority.health()
                _emit({"authority": health, "maintenance": maintenance.health(), "tick": tick})
                return 0 if health["fresh"] and maintenance.health()["ok"] and not tick["unfinished"] else 1
            projector = _projector(config)
            if args.rebuild:
                projector.reset()
            pump = ProjectionPump(authority, projector)
            processed = 0
            for _ in range(args.max_batches):
                result = pump.run_batch(limit=args.batch_size)
                processed += result["processed"]
                if result["processed"] < args.batch_size:
                    break
            _emit({"processed": processed, **projector.health()})
            return 0
    except KeyboardInterrupt:
        return 130
    except SystemExit as exit:
        if exit.code is None or exit.code == 0:
            return 0
        print("service_exit: Server startup or shutdown failed", file=sys.stderr)
        return 1
    except ConfigError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 2
    except (AuthorityError, TaskClientError, JournalError, ClockError, PalaceError,
            ProjectionError, ExecutionError, DiscoveryError, PlatformError) as error:
        # Never print exception details, full requests, credential paths or config blobs.
        code = error.code if re.fullmatch(r"[a-z0-9_]+", error.code) else "operation_failed"
        print(f"{code}: Task operation failed; inspect authority diagnostics before retrying", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("operation_failed: Cannot complete the configured task operation", file=sys.stderr)
        return 1
    except Exception:
        print("internal_error: Task service failed; no exception payload was disclosed", file=sys.stderr)
        return 1
