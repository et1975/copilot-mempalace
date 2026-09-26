"""Owned SDK HTTP service and actual task authority, with a controlled upstream."""

import asyncio
import json
from pathlib import Path
import socket
import threading

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from authority_fixture import AUTHORITY, Clock, LogClient, genesis, state_directory
from mempalace_tasks.authority import TaskAuthority
from mempalace_tasks.config import load_config
from mempalace_tasks.lifecycle import HttpLifecycle
from mempalace_tasks.maintenance import LeaseMaintenance
from mempalace_tasks.server import create_app
from mempalace_tasks.server_identity import InstanceIdentity
from test_config import config_document


TOKEN = "owned-service-test-token"


class SocketRunner:
    """Own uvicorn's socket/stop handle for real SDK integration."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(64)
        self.port = self.socket.getsockname()[1]
        self.ready = threading.Event()
        self.server = None
        self.errors = []

    def run(self, app, **_):
        async def dispatch(scope, receive, send):
            async def observed(message):
                await send(message)
                if message["type"] == "lifespan.startup.complete":
                    self.ready.set()
                if message["type"] == "lifespan.startup.failed":
                    self.errors.append(message.get("message"))
                    self.ready.set()
            await app(scope, receive, observed)
        self.server = uvicorn.Server(uvicorn.Config(
            dispatch, log_config=None, log_level="critical", access_log=False,
            ws="none", lifespan="on", interface="asgi3", timeout_graceful_shutdown=3,
        ))
        try:
            asyncio.run(self.server.serve(sockets=[self.socket]))
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()

    def stop(self):
        if self.server is not None:
            self.server.should_exit = True


class ServiceFixture:
    def __init__(self, *, policy=None, genesis_fields=None):
        self.directory = state_directory()
        self.log = LogClient()
        self.clock = Clock()
        initial = genesis()
        initial.update(genesis_fields or {})
        initial["policy"] = {"sweep_seconds": 1, **(policy or {})}
        root = Path(self.directory.name)
        document = config_document(root)
        document["genesis"] = {key: value for key, value in initial.items()
                               if key not in {"operation", "command_id"}}
        token = root / "service.token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps(document))
        self.config = load_config(self.config_path)
        self.authority = TaskAuthority(AUTHORITY, self.log, self.config.runtime_dir,
                                       clock=self.clock, initialize=True, backoff=lambda _: None).start()
        self.authority.execute_current(initial)
        self.epoch = self.authority.epoch_id
        self.maintenance = LeaseMaintenance(self.authority.bound_current(), self.clock,
                                           system_actor="system", recovery_actor="operator")
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(64)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        document["port"] = self.port
        self.config_path.write_text(json.dumps(document))
        self.config = load_config(self.config_path)
        self.identity = InstanceIdentity(AUTHORITY, self.epoch, self.url)
        self.lifecycle = HttpLifecycle(self.config, self.identity, shutdown=self.stop)
        try:
            self.app = create_app(self.authority, self.maintenance, token=TOKEN,
                                  port=self.port, lifecycle=self.lifecycle)
        except BaseException:
            self.socket.close()
            self.authority.close()
            self.directory.cleanup()
            raise
        self.ready = threading.Event()
        self.errors = []
        fixture = self
        class FixtureServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if not self.started:
                    raise AssertionError("Owned listener did not start")
                fixture.lifecycle.publish()
                fixture.ready.set()

        self.server = FixtureServer(uvicorn.Config(
            self.dispatch, log_config=None, log_level="critical", access_log=False,
            ws="none", lifespan="on", interface="asgi3", timeout_graceful_shutdown=3,
        ))
        self.thread = threading.Thread(target=self.run, name="owned-task-service")

    async def dispatch(self, scope, receive, send):
        async def observed(message):
            await send(message)
            if message["type"] == "lifespan.startup.failed":
                self.errors.append(message.get("message"))
                self.ready.set()
        await self.app(scope, receive, observed)

    def run(self):
        try:
            asyncio.run(self.server.serve(sockets=[self.socket]))
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(5) or self.errors:
            self.__exit__(None, None, None)
            raise AssertionError(f"Service failed to start: {self.errors}")
        return self

    def __exit__(self, *_):
        self.server.should_exit = True
        if self.thread.ident:
            self.thread.join(8)
        self.socket.close()
        self.lifecycle.clear()
        self.authority.close()
        if not self.thread.is_alive():
            self.directory.cleanup()
        if self.thread.is_alive() or self.errors:
            raise AssertionError(f"Service failed to stop: {self.errors}")

    def stop(self):
        self.server.should_exit = True

    def execute(self, command):
        """Fixture-issued domain command, frozen to this fixture's original epoch."""
        return self.authority.execute(command, expected_epoch=self.epoch)

    def sdk(self, name=None, arguments=None):
        async def request():
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False, timeout=4,
            ) as http:
                async with streamable_http_client(self.url, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        return (await session.list_tools() if name is None
                                else await session.call_tool(name, arguments or {}))
        return asyncio.run(request())
