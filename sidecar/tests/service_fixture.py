"""Owned SDK HTTP service and actual task authority, with a controlled upstream."""

import asyncio
import socket
import threading

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from authority_fixture import AUTHORITY, Clock, LogClient, genesis, state_directory
from mempalace_tasks.authority import TaskAuthority
from mempalace_tasks.maintenance import LeaseMaintenance
from mempalace_tasks.server import create_app


TOKEN = "owned-service-test-token"


class SocketRunner:
    """Own uvicorn's socket/stop handle while exercising the real foreground CLI."""

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
    def __init__(self, *, projector=None, policy=None, genesis_fields=None):
        self.directory = state_directory()
        self.log = LogClient()
        self.clock = Clock()
        self.authority = TaskAuthority(AUTHORITY, self.log, self.directory.name,
                                       clock=self.clock, backoff=lambda _: None).start()
        initial = genesis()
        initial.update(genesis_fields or {})
        initial["policy"] = {"sweep_seconds": 1, **(policy or {})}
        self.authority.execute(initial)
        self.maintenance = LeaseMaintenance(self.authority, self.clock,
                                            system_actor="system", recovery_actor="operator")
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(64)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.projector = projector(self) if callable(projector) else projector
        try:
            self.app = create_app(self.authority, self.maintenance, token=TOKEN,
                                  port=self.port, projector=self.projector)
        except BaseException:
            self.socket.close()
            self.authority.close()
            self.directory.cleanup()
            raise
        self.ready = threading.Event()
        self.errors = []
        self.server = uvicorn.Server(uvicorn.Config(
            self.dispatch, log_config=None, log_level="critical", access_log=False,
            ws="none", lifespan="on", interface="asgi3", timeout_graceful_shutdown=3,
        ))
        self.thread = threading.Thread(target=self.run, name="owned-task-service")

    async def dispatch(self, scope, receive, send):
        async def observed(message):
            await send(message)
            if message["type"] == "lifespan.startup.complete":
                self.ready.set()
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
        self.authority.close()
        if not self.thread.is_alive():
            self.directory.cleanup()
        if self.thread.is_alive() or self.errors:
            raise AssertionError(f"Service failed to stop: {self.errors}")

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
