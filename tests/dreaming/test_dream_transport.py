"""Sanctioned local writer selection and fail-closed uncertainty."""
import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest


def api():
    assert importlib.util.find_spec("dream_transport"), "Sanctioned Dreaming transport is not implemented"
    return importlib.import_module("dream_transport")


def fake_server(palace, *, refusal=None):
    calls = []
    server = SimpleNamespace(
        _config=SimpleNamespace(palace_path=str(palace)), _MCP_WRITER_LOCK_CM=None,
        _mcp_tool_preflight_refusal=lambda request, name: refusal,
        _release_mcp_writer_lock=lambda: calls.append("release"),
        TOOLS={"mempalace_event_append": {"handler": lambda **kwargs: {"success": True, "event": kwargs}},
               "mempalace_add_drawer": {"handler": lambda **kwargs: {"success": True, "id": "drawer"}}},
    )
    return server, calls


def test_control_never_forwards_to_hub(tmp_path, monkeypatch):
    module = api()
    import dream_palace
    server, calls = fake_server(tmp_path)
    monkeypatch.setattr(dream_palace, "_embedded_mcp_server", lambda path: server)
    monkeypatch.setattr(module, "_hub_info", lambda path: pytest.fail("control queried hub"))
    result = module.call_tool(str(tmp_path), "mempalace_event_append", {"body": "exact"})
    assert result["event"]["body"] == "exact"
    assert calls == ["release"]


def test_embedded_preflight_refusal_is_not_bypassed(tmp_path, monkeypatch):
    module = api()
    import dream_palace
    server, calls = fake_server(tmp_path, refusal={"error": {"message": "foreign stdio writer"}})
    monkeypatch.setattr(dream_palace, "_embedded_mcp_server", lambda path: server)
    monkeypatch.setattr(module, "_hub_info", lambda path: None)
    with pytest.raises(module.TransportError, match="refused"):
        module.call_tool(str(tmp_path), "mempalace_add_drawer", {}, vector=True)
    assert calls == ["release"]


def test_nested_lock_reuses_existing_palace_lock(tmp_path):
    module = api()
    from dream_palace import palace_mutation_lock
    with palace_mutation_lock(str(tmp_path), timeout_seconds=0):
        with module.mutation_lock(str(tmp_path), timeout_seconds=0):
            with module.mutation_lock(str(tmp_path), timeout_seconds=0):
                pass


def test_uncertain_hub_reply_never_uses_embedded_fallback(tmp_path, monkeypatch):
    module = api()
    import dream_palace
    monkeypatch.setattr(module, "_hub_info", lambda path: {"palace_path": str(tmp_path)})
    monkeypatch.setattr(module, "_hub_call", lambda *args: (_ for _ in ()).throw(module.UncertainWriteError("lost")))
    monkeypatch.setattr(dream_palace, "_embedded_mcp_server", lambda path: pytest.fail("alternate writer"))
    with pytest.raises(module.UncertainWriteError):
        module.call_tool(str(tmp_path), "mempalace_add_drawer", {}, vector=True)


@pytest.mark.parametrize("info", [
    {"host": "example.org", "port": 8000, "palace_path": "PALACE"},
    {"host": "127.0.0.1", "port": 8000, "palace_path": "/wrong-palace"},
    {"host": "127.0.0.1", "port": 8000, "palace_path": "PALACE", "read_only": True},
])
def test_hub_discovery_rejects_remote_wrong_palace_or_readonly(tmp_path, monkeypatch, info):
    module = api()
    from mempalace import server_registry
    info = {**info, "palace_path": str(tmp_path) if info["palace_path"] == "PALACE" else info["palace_path"]}
    monkeypatch.setattr(server_registry, "read_live_serverinfo", lambda palace: info)
    with pytest.raises(module.TransportError):
        module._hub_info(str(tmp_path))


def test_hub_authentication_and_strict_response(tmp_path, monkeypatch):
    module = api()
    from mempalace import server_registry
    monkeypatch.setattr(server_registry, "load_server_token", lambda palace: "test-token")
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
                {"type": "text", "text": json.dumps({"success": True, "id": "drawer"})}
            ]}}).encode()
    def open_request(request, **kwargs):
        requests.append(request)
        return Response()
    monkeypatch.setattr(module.urllib.request, "build_opener",
                        lambda *handlers: SimpleNamespace(open=open_request))
    result = module._hub_call(str(tmp_path), {"host": "127.0.0.1", "port": 1234},
                             "mempalace_add_drawer", {"content": "exact"})
    assert result["id"] == "drawer"
    assert requests[-1].get_header("Authorization") == "Bearer test-token"
    assert json.loads(requests[-1].data)["params"]["arguments"] == {"content": "exact"}


def test_hub_missing_authentication_token_is_blocked(tmp_path, monkeypatch):
    module = api()
    from mempalace import server_registry
    monkeypatch.setattr(server_registry, "load_server_token", lambda palace: "")
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unauthenticated request"))
    with pytest.raises(module.TransportError, match="token|auth"):
        module._hub_call(str(tmp_path), {"host": "127.0.0.1", "port": 1234},
                         "mempalace_add_drawer", {})


def test_malformed_success_reply_is_uncertain_not_a_settled_refusal(tmp_path, monkeypatch):
    module = api()
    from mempalace import server_registry
    monkeypatch.setattr(server_registry, "load_server_token", lambda palace: "test-token")
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"id": 1, "result": {"content": [
                {"type": "text", "text": "[]"}
            ]}}).encode()
    monkeypatch.setattr(module.urllib.request, "build_opener",
                        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: Response()))
    with pytest.raises(module.UncertainWriteError):
        module._hub_call(str(tmp_path), {"host": "127.0.0.1", "port": 1234},
                         "mempalace_add_drawer", {})


def test_uncertain_exception_exposes_effect_classification():
    module = api()
    assert module.UncertainWriteError("reply lost").uncertain is True
    assert not getattr(module.TransportError("explicit refusal"), "uncertain", False)


def not_dispatched_type(module):
    assert hasattr(module, "NotDispatchedError"), "Positive pre-dispatch refusal marker is missing"
    return module.NotDispatchedError


@pytest.mark.parametrize("refusal", ["preflight", "capability", "binding"])
def test_embedded_refusal_proves_handler_was_not_dispatched(tmp_path, monkeypatch, refusal):
    module = api()
    error_type = not_dispatched_type(module)
    import dream_palace
    server, calls = fake_server(
        tmp_path, refusal={"error": {"message": "foreign writer"}} if refusal == "preflight" else None)
    dispatched = []
    server.TOOLS["mempalace_add_drawer"]["handler"] = lambda **kwargs: dispatched.append(True)
    if refusal == "capability":
        server.TOOLS.pop("mempalace_add_drawer")
    elif refusal == "binding":
        server._config.palace_path = str(tmp_path / "wrong")
    monkeypatch.setattr(dream_palace, "_embedded_mcp_server", lambda path: server)
    monkeypatch.setattr(module, "_hub_info", lambda path: None)
    with pytest.raises(error_type):
        module.call_tool(str(tmp_path), "mempalace_add_drawer", {}, vector=True)
    assert dispatched == []


@pytest.mark.parametrize("failure", ["exception", "result_false", "malformed", "nested_refusal"])
def test_post_handler_failure_never_claims_no_dispatch(tmp_path, monkeypatch, failure):
    module = api()
    error_type = not_dispatched_type(module)
    import dream_palace
    server, calls = fake_server(tmp_path)
    def handler(**kwargs):
        calls.append("handler")
        if failure == "exception":
            raise module.TransportError("native failure after possible effects")
        if failure == "nested_refusal":
            raise module.NotDispatchedError("nested refusal after outer handler effects")
        if failure == "result_false":
            return {"success": False, "error": "post-effect verification failed"}
        return []
    server.TOOLS["mempalace_add_drawer"]["handler"] = handler
    monkeypatch.setattr(dream_palace, "_embedded_mcp_server", lambda path: server)
    monkeypatch.setattr(module, "_hub_info", lambda path: None)
    with pytest.raises(module.TransportError) as raised:
        module.call_tool(str(tmp_path), "mempalace_add_drawer", {}, vector=True)
    assert not isinstance(raised.value, error_type)
    assert calls == ["handler", "release"]


@pytest.mark.parametrize("refusal", ["remote", "wrong_palace", "read_only", "no_token"])
def test_local_discovery_refusal_is_typed_before_http_dispatch(tmp_path, monkeypatch, refusal):
    module = api()
    error_type = not_dispatched_type(module)
    from mempalace import server_registry
    info = {"host": "127.0.0.1", "port": 1234, "palace_path": str(tmp_path)}
    if refusal == "remote":
        info["host"] = "example.org"
    elif refusal == "wrong_palace":
        info["palace_path"] = str(tmp_path / "other")
    elif refusal == "read_only":
        info["read_only"] = True
    monkeypatch.setattr(server_registry, "read_live_serverinfo", lambda palace: info)
    monkeypatch.setattr(server_registry, "load_server_token", lambda palace: "")
    monkeypatch.setattr(module.urllib.request, "build_opener",
                        lambda *handlers: pytest.fail("HTTP dispatch must not be reached"))
    with pytest.raises(error_type):
        module.call_tool(str(tmp_path), "mempalace_add_drawer", {}, vector=True)


@pytest.mark.parametrize("response_kind", ["rpc_error", "tool_error", "result_false", "http_error"])
def test_http_error_response_is_not_positive_no_dispatch_proof(tmp_path, monkeypatch, response_kind):
    module = api()
    error_type = not_dispatched_type(module)
    from mempalace import server_registry
    monkeypatch.setattr(server_registry, "load_server_token", lambda palace: "test-token")
    dispatched = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            if response_kind == "rpc_error":
                payload = {"id": 1, "error": {"message": "server error"}}
            elif response_kind == "tool_error":
                payload = {"id": 1, "result": {"isError": True}}
            else:
                payload = {"id": 1, "result": {"content": [
                    {"type": "text", "text": '{"success":false,"error":"post-effect failure"}'}
                ]}}
            return json.dumps(payload).encode()
    def open_request(request, **kwargs):
        dispatched.append(True)
        if response_kind == "http_error":
            raise module.urllib.error.HTTPError(request.full_url, 503, "error", {}, None)
        return Response()
    monkeypatch.setattr(module.urllib.request, "build_opener",
                        lambda *handlers: SimpleNamespace(open=open_request))
    with pytest.raises(module.TransportError) as raised:
        module._hub_call(str(tmp_path), {"host": "127.0.0.1", "port": 1234},
                         "mempalace_add_drawer", {})
    assert not isinstance(raised.value, error_type)
    assert dispatched == [True]
