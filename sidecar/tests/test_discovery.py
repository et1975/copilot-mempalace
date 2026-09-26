"""Real loopback HTTP/identity checks against disposable private fixtures."""

from dataclasses import replace
import json
import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from mempalace_tasks import discovery
from mempalace_tasks import platform_support as platform
from mempalace_tasks.server_identity import InstanceIdentity
from portable_lifecycle_fixture import ConfigurationFixture, IdentityServer, ROOT_TOKEN


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ConfigurationFixture()
        self.addCleanup(self.fixture.close)
        self.config = self.fixture.config

    def server(self, **kwargs):
        fixture = IdentityServer(self.config, **kwargs)
        self.addCleanup(fixture.close)
        fixture.publish()
        return fixture

    def error(self, code, action):
        with self.assertRaises(discovery.DiscoveryError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(ROOT_TOKEN, repr(caught.exception))
        return caught.exception

    def mutate(self, **updates):
        path = self.config.runtime_dir / "serverinfo.json"
        document = json.loads(platform.read_regular(path, private=True))
        platform.atomic_write_cache(path, json.dumps({**document, **updates}).encode())

    def test_missing_registry_does_not_create_runtime_or_connect_to_fixed_fallback(self):
        before = set(self.fixture.root.iterdir())
        self.error("registry_missing", lambda: discovery.connect(self.config, timeout=0.3))
        self.assertEqual(set(self.fixture.root.iterdir()), before)

    def test_authenticated_connection_is_instance_scoped_and_public_output_is_redacted(self):
        server = self.server()
        info = discovery.connect(self.config, timeout=2)
        self.assertEqual(info.url, server.identity.endpoint)
        self.assertEqual(info.instance_id, server.identity.instance_id)
        self.assertEqual(info.authority_id, self.config.authority_id)
        self.assertTrue(info.ready)
        self.assertNotEqual(info.token, ROOT_TOKEN)
        public = info.public()
        self.assertEqual(set(public), {"url", "authority_id", "instance_id", "ready"})
        self.assertNotIn(info.token, repr(info))
        self.assertNotIn(ROOT_TOKEN, repr(info) + json.dumps(public))
        self.assertNotIn("Authorization", server.requests[0][2])
        self.assertRegex(server.requests[0][1], r"^/identity\?nonce=[0-9a-f]{64}$")
        record = platform.read_regular(self.config.runtime_dir / "serverinfo.json", private=True)
        self.assertNotIn(ROOT_TOKEN.encode(), record)
        self.assertNotIn(info.token.encode(), record)
        self.assertEqual(json.loads(record)["pid"], os.getpid())

    def test_ready_false_is_not_a_successful_connection(self):
        self.server(ready=False)
        self.error("not_ready", lambda: discovery.connect(self.config))

    def test_registry_binding_covers_authority_hub_runtime_and_server_configuration(self):
        server = self.server()
        for updated in (
            replace(self.config, authority_id=str(uuid4())),
            replace(self.config, hub_url="http://127.0.0.1:2/mcp"),
            replace(self.config, host="127.0.0.2"),
            replace(self.config, port=1),
            replace(self.config, lifecycle="external"),
            replace(self.config, hub_token_file=self.fixture.root / "another-hub.token"),
            replace(self.config, configuration={**self.config.configuration, "extra": True}),
        ):
            with self.subTest(config=repr(updated)):
                self.error("binding_mismatch", lambda: discovery.connect(updated))
        self.assertFalse(server.requests)

    def test_wrong_binding_never_sends_request(self):
        server = self.server()
        self.mutate(binding="0" * 64)
        self.error("binding_mismatch", lambda: discovery.connect(self.config))
        self.assertFalse(server.requests)

    def test_unsafe_registry_endpoints_fail_before_network(self):
        server = self.server()
        for endpoint in ("http://example.com:80/mcp", "http://127.0.0.1:80/mcp?x=1",
                         "http://u:p@127.0.0.1:80/mcp", "http://127.0.0.1:0/mcp",
                         "http://127.0.0.1:80/mcp#x", "http://127.0.0.2:80/mcp"):
            with self.subTest(endpoint=endpoint):
                self.mutate(endpoint=endpoint)
                self.error("invalid_registry", lambda: discovery.connect(self.config))
        self.assertFalse(server.requests)

    def test_stale_port_with_unrelated_server_never_receives_reusable_token(self):
        server = self.server(mode="unrelated")
        self.error("invalid_identity_response", lambda: discovery.connect(self.config))
        self.assertNotIn("Authorization", server.requests[0][2])
        self.assertNotIn(ROOT_TOKEN, repr(server.requests))

    def test_wrong_proof_authority_instance_endpoint_and_replayed_nonce_are_rejected(self):
        server = self.server()
        for mode in ("wrong-authority", "wrong-instance", "wrong-endpoint"):
            server.mode = mode
            with self.subTest(mode=mode):
                self.error("identity_mismatch", lambda: discovery.connect(self.config))
        server.mode = "normal"
        discovery.connect(self.config)
        server.mode = "replay"
        self.error("identity_mismatch", lambda: discovery.connect(self.config))

    def test_strict_registry_schema_and_json_corruption_are_explicit(self):
        self.server()
        path = self.config.runtime_dir / "serverinfo.json"
        original = platform.read_regular(path, private=True)
        for data in (b"{}", b"null", b'{"schema_version":1,"schema_version":1}',
                     b"\xff", b" " * 8193):
            with self.subTest(data=data[:30]):
                platform.atomic_write_cache(path, data)
                self.error("invalid_registry", lambda: discovery.connect(self.config))
        platform.atomic_write_cache(path, original)
        self.mutate(pid=True)
        self.error("invalid_registry", lambda: discovery.connect(self.config))

    def test_malformed_nonfinite_and_oversized_http_bodies_are_rejected(self):
        server = self.server()
        for mode, code in (("malformed", "invalid_response"), ("nonfinite", "invalid_response"),
                           ("oversized", "body_too_large")):
            with self.subTest(mode=mode):
                server.mode = mode
                self.error(code, lambda: discovery.connect(self.config))

    def test_environment_proxies_are_ignored_and_redirects_never_followed(self):
        server = self.server()
        trap = IdentityServer(self.config, own=False, mode="unrelated")
        self.addCleanup(trap.close)
        proxy = trap.identity.endpoint.removesuffix("/mcp")
        with patch.dict(os.environ, {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                                     "ALL_PROXY": proxy, "NO_PROXY": "",
                                     "http_proxy": proxy, "no_proxy": ""}):
            self.assertTrue(discovery.connect(self.config).ready)
            server.mode, server.redirect_url = "redirect", proxy + "/stolen"
            self.error("http_error", lambda: discovery.connect(self.config))
        self.assertFalse(trap.requests)

    def test_whole_operation_deadline_interrupts_slow_drip(self):
        self.server(mode="slow")
        started = time.monotonic()
        self.error("timeout", lambda: discovery.connect(self.config, timeout=0.15))
        self.assertLess(time.monotonic() - started, 0.7)

    def test_timeout_is_positive_finite_and_bounded(self):
        for timeout in (True, 0, -1, float("inf"), float("nan"), 301, "1", 10**10000):
            with self.subTest(kind=type(timeout).__name__):
                self.error("invalid_timeout", lambda: discovery.connect(self.config, timeout=timeout))

    def test_json_response_must_be_utf8_not_autodetected_utf16(self):
        self.server(mode="utf16")
        self.error("invalid_response", lambda: discovery.connect(self.config))

    def test_chunked_oversized_body_is_bounded_without_content_length(self):
        self.server(mode="chunked-oversized")
        self.error("body_too_large", lambda: discovery.connect(self.config))

    def test_stale_closed_endpoint_has_no_implicit_fallback(self):
        server = self.server()
        server.close()
        self.error("endpoint_unavailable", lambda: discovery.connect(self.config))
        self.assertEqual(server.requests, [])

    def test_credential_rotation_rejects_cached_proof_without_changing_binding(self):
        self.server()
        self.error("identity_mismatch", lambda: discovery.connect(
            replace(self.config, service_token="rotated-local-only-credential")))

    def test_registry_identity_tampering_cannot_choose_another_authority_or_instance(self):
        server = self.server()
        self.mutate(instance_id=str(uuid4()))
        self.error("identity_mismatch", lambda: discovery.connect(self.config))
        self.mutate(authority_id=str(uuid4()))
        self.error("invalid_registry", lambda: discovery.connect(self.config))
        self.assertEqual(len(server.requests), 1)

    def test_publish_rejects_identity_not_bound_to_config(self):
        platform.ensure_private_directory(self.config.runtime_dir)
        identity = InstanceIdentity(str(uuid4()), str(uuid4()), "http://127.0.0.1:1/mcp")
        self.error("binding_mismatch", lambda: discovery.publish_registry(self.config, identity))
        self.assertFalse((self.config.runtime_dir / "serverinfo.json").exists())

    def test_connect_with_external_lifecycle_still_never_starts_anything(self):
        config = replace(self.config, lifecycle="external")
        self.error("registry_missing", lambda: discovery.connect(config))
        self.assertFalse(config.runtime_dir.exists())
