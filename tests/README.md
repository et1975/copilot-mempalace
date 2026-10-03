# Repository tests

Pytest is the canonical runner for all repository tests. Existing unittest
assertions and function-style tests run together; their integration prerequisites
and gates are unchanged. Tests and test-only helpers are not deployed with hooks,
skills or sidecar distributions.

## Layout

| Location | Migrated contents | Additional prerequisites |
|---|---|---|
| [`hooks/`](hooks/) | Transcript-adapter, session-finalization configuration and optional procedural-adapter tests | Existing optional parser checks; installed MemPalace/model for procedural integration |
| [`sidecar/`](sidecar/) | 32 test modules and 5 worker/fixture helpers | Sidecar production dependencies; preinstalled `mempalace-mcp` for the explicit live-hub gate |
| [`dreaming/`](dreaming/) | 26 migrated modules plus captured-source and nonpublishing-draft tests | Existing MemPalace installation and local model requirements for integration tests |
| [`mempalace-backup/`](mempalace-backup/) | 3 backup/wing test modules | Sidecar production dependencies |

Root test modules additionally check layout, import targets, harness isolation
and distribution contents. The migrated counts describe files, not executed
test cases. New foundation tests remain here, not beside deployable scripts.

Production modules remain under `hooks/`, `sidecar/src/`,
`skills/dreaming/scripts/` and `skills/mempalace-backup/scripts/`. Root
[`pytest.ini`](../pytest.ini) supplies these source import paths and the shared
test paths. Run from the repository root without manually setting `PYTHONPATH`
or changing into production directories. Test directories are not packages;
keep module basenames unique and worker helpers beside their sidecar tests.

## Prepared environment

Set `TEST_PY` to the absolute path of an already provisioned Python **3.11+**
interpreter. It must have **pytest 8.4.2**, pinned only in
[`requirements-test.txt`](../requirements-test.txt). Pytest is a development
dependency, not part of `sidecar/pyproject.toml` or `sidecar/requirements.lock`.

A full-suite environment also needs the sidecar's declared production
dependencies and the existing MemPalace/model prerequisites in that interpreter.
It additionally requires preinstalled `uv` on PATH and the sidecar's
[`[build-system]` prerequisites](../sidecar/pyproject.toml) in `TEST_PY`:
`setuptools>=68`, plus `wheel` if the chosen backend requires it. These are
development/build prerequisites, not runtime dependencies.

[`test_distribution.py`](test_distribution.py) is part of the default root
suite. It builds a real wheel and source distribution, then rebuilds a wheel
from that source distribution, offline in external temporary storage. The
package regression requires the preinstalled build tools above.

Keep required local models available through the existing cache/setup. No test
command installs dependencies, downloads packages/models or bootstraps a runtime.
Prepare prerequisites separately using the project's approved provisioning
process.

An environment with only pytest can validate explicitly selected dependency-free
tests, **not** the entire suite. Missing imports/models must be reported as
unavailable prerequisites, not hidden with new skips or collection exclusions.
Report the exact selection, failures and skip reasons; successful collection or a
partial run is not full-suite validation.

Task-setup regressions in `tests/test_task_setup.py` and
`tests/test_task_setup_runtime.py` run directly in Python,
using the same prepared sidecar environment. They exercise isolated fixtures,
not the user's real task authority, and require no F#/.NET runtime. Native
platform-specific cases report their own prerequisites; the setup suite is
not skipped wholesale for lack of .NET or for running outside Linux.

## External storage and bytecode

Set `SESSION_FILES` to an **existing absolute directory outside the checkout**
for session artifacts. The commands below assume both it and `TEST_PY` are set.
Export these once before collection, tests and installed-CLI verification:

```bash
export PYTHONDONTWRITEBYTECODE=1
export DREAMING_TEST_TMPDIR="$SESSION_FILES"
export MPTASK_TEST_TMPDIR="$SESSION_FILES"
export TMPDIR="$SESSION_FILES"
```

The bytecode environment variable is inherited by child Python processes;
`python -B` alone does not protect subprocesses. Keep it in constructed child
environments as well.

Every `--basetemp` below is a **dedicated disposable child** of `SESSION_FILES`.
Pytest clears that child. Never pass the session artifact directory itself,
a live palace, or a directory containing evidence you want to retain. Use
separate children for independent runs.

The root harness validates explicit temporary roots before collection, including
`--basetemp`, `TMPDIR`, `TEMP`, `TMP`, `DREAMING_TEST_TMPDIR` and
`MPTASK_TEST_TMPDIR`. Invalid or repository-contained roots, including symlink
aliases, are errors. Explicit DREAMING/MPTASK roots must already be directories;
pytest may create the basetemp child.

When test-specific roots are omitted, the harness defaults them to its external
pytest basetemp. It sets stdlib temporary storage there for the test session and
restores the environment afterward. HOME is preserved globally for installed
model-cache lookup. Only the backup/wing subtree isolates HOME/USERPROFILE and
clears palace overrides per test. Tests use disposable storage, never the user's
live palace.

The session fixture pins `DREAMING_TEST_MODEL_CACHE` to the original HOME's
`.cache/chroma/onnx_models/all-MiniLM-L6-v2` before per-test HOME isolation.
An explicit value is preserved; it names the directory containing `onnx/`.
This only selects an existing cache and does not create or download a model.
Installed procedural tests fail explicitly when required cache files are absent.
The optional adapter tests use the same validated `DREAMING_TEST_TMPDIR` as
other dreaming tests; no separate `PROCEDURAL_TEST_ROOT` setup is needed.

## Commands

Run these from the repository root after the exports above.

```bash
# Collect all suites without executing test cases.
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-collect" --collect-only -q

# Run the complete suite with its existing gates and platform skips.
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-all" -q
```

Select the smallest relevant suite while developing:

```bash
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-hooks" tests/hooks -q
"$TEST_PY" -W error -m pytest --basetemp "$SESSION_FILES/pytest-sidecar" tests/sidecar -q
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-dreaming" tests/dreaming -q
"$TEST_PY" -W error -m pytest --basetemp "$SESSION_FILES/pytest-backup" tests/mempalace-backup -q

# Harness/layout regressions.
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-harness" \
  tests/test_layout.py tests/test_harness.py -q

# Task setup and its native subprocess/runtime boundary.
"$TEST_PY" -W error -m pytest --basetemp "$SESSION_FILES/pytest-task-setup" \
  tests/test_task_setup.py tests/test_task_setup_runtime.py -q

# Procedural command/replay integration.
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-procedural" \
  tests/dreaming/test_dream_procedure.py \
  tests/dreaming/test_procedural_replay.py -q

# Captured sources, health/drafts, disabled adapter and restore/replay protection.
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-procedural-foundation" \
  tests/dreaming/test_dream_procedur*.py tests/dreaming/test_dream_metadata.py \
  tests/dreaming/test_dream_restore.py \
  tests/dreaming/test_procedural_replay.py tests/hooks/test_procedural_context.py \
  tests/test_harness.py tests/test_layout.py tests/mempalace-backup -q
```

The existing optional real-hub gate remains explicit:

```bash
MPTASK_LIVE_HUB=1 "$TEST_PY" -W error -m pytest \
  --basetemp "$SESSION_FILES/pytest-live-hub" tests/sidecar/test_live_contract.py -q
```

That gate requires preinstalled `mempalace-mcp`. Its fixture isolates HOME and
palace storage, binds port zero, validates child-owned registry records and stops
only its own processes. Other integration suites retain their existing
prerequisites; they have not been made opt-in. Existing optional-hook and platform
skips are not passing tests, and Linux results do not certify native macOS/Windows
behavior.

Root configuration disables pytest's repository-local cache and uses `prepend`
imports to preserve existing bare-module identities. Do not add parallel
execution: tests share process-global module and environment state.

## GitHub Actions CI

The [`CI` workflow](../.github/workflows/ci.yml) runs on pushes to `main`, pull
requests and manual `workflow_dispatch` runs. It uses Ubuntu 24.04 with Python
3.12 and read-only repository permissions. Official actions are pinned to commit
SHAs; CI dependency pins live in [`requirements-ci.txt`](../requirements-ci.txt),
with the pytest pin retained in
[`requirements-test.txt`](../requirements-test.txt). CI installs the full
transitive [`requirements-ci.lock`](../requirements-ci.lock) with hash
verification. After changing any input requirements, regenerate that lock with
the `uv` version pinned in `requirements-ci.txt`:

```bash
uv pip compile requirements-ci.txt --python-version 3.12 --universal \
  --only-binary=:all: --generate-hashes --output-file requirements-ci.lock
```

Provisioning is separate from test execution: CI prepares the Python/runtime and
build prerequisites, including `uv`, and preprovisions the MiniLM model cache
before running tests. These preparation steps may access external package/model
sources; the tests do not install missing prerequisites. In particular, the
wheel/sdist build and wheel-from-sdist roundtrip remain offline.

CI runs the full root pytest suite serially, including the distribution
regression, with an isolated temporary HOME and external disposable test
storage. The prepared model cache is available in that isolated environment;
no real palace, user credentials or publishing step is required or used.
Existing integration gates and platform skips remain intact:
`MPTASK_LIVE_HUB=1` is an explicit opt-in, not part of default CI. Linux CI does
not certify native Windows or macOS behavior.

The [local full-suite command](#commands) remains canonical. Local runs still
require the [already provisioned environment](#prepared-environment); CI setup
does not change the tests into an installer or replace the separate
[deployment acceptance checks](#distribution-and-deployment-acceptance).

## Distribution and deployment acceptance

A passing source-tree test run does not prove the deployed artifacts are clean.
The default suite's package regression exercises real offline builds and archive
contents; installed-CLI smoke and deployment-tree inspection remain separate
acceptance checks. Before accepting packaging changes:

1. Build the sidecar offline with preinstalled build tools/backends, using a clean
   source staging tree under `SESSION_FILES` so stale `egg-info/SOURCES.txt`
   cannot influence the result. Keep archives and build output outside the
   repository.
2. Inspect the actual wheel and source-distribution member names. Neither may
   contain test directories, test modules, the five known sidecar test helpers,
   pytest configuration or `requirements-test.txt`. Match test assets by their
   paths/identities, not by rejecting every filename containing `test`.
3. Require application modules and console entry-point metadata in the wheel;
   require README, the setup guide, requirements lock, build metadata and application source in
   the source distribution. [`sidecar/MANIFEST.in`](../sidecar/MANIFEST.in)
   includes README, `setup.md` and the production lock and explicitly prunes local tests;
   sibling root tests and development dependencies are not package inputs.
4. Rebuild a wheel from the produced source distribution offline. Install that
   wheel into a disposable already-provisioned environment without fetching
   dependencies. From outside the checkout with `PYTHONPATH` cleared and
   `PYTHONDONTWRITEBYTECODE=1` still exported, verify both
   `mempalace-tasks --help` and `mempalace-tasks mcp --help`, plus
   `mempalace-tasks setup --help` and `python -m mempalace_tasks setup --help`
   using that environment's Python. Help is an installed-entry-point check,
   not proof of a configured or reachable task authority.
5. Inspect clean copies of deployable `hooks/` and `skills/`: runtime scripts and
   their referenced files remain present, no tests/helpers remain, and runtime
   modules do not import repository tests.

Record archive inspection and installed-smoke results separately from source
tests. Missing offline build/runtime prerequisites leave the affected gate
blocked or unrun, not passed.
