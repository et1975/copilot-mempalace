import copy
import unittest

from mempalace_tasks.server_identity import (
    IdentityError,
    InstanceIdentity,
    instance_bearer,
    make_proof,
    verify_proof,
)


AUTHORITY = "11111111-1111-4111-8111-111111111111"
INSTANCE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
ENDPOINT = "http://127.0.0.1:8766/mcp"
SECRET = "owned-identity-test-secret"
NONCE = "a1" * 32


class ServerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = InstanceIdentity(AUTHORITY, INSTANCE, ENDPOINT)

    def test_verified_proof_preserves_ready_and_not_ready_states(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                proof = make_proof(self.identity, SECRET, NONCE, ready=ready)
                self.assertIs(verify_proof(self.identity, SECRET, NONCE, proof), ready)
                self.assertNotIn(SECRET, repr(proof))
                self.assertNotIn(instance_bearer(self.identity, SECRET), repr(proof))

    def test_tampered_or_wrong_context_is_never_accepted(self):
        proof = make_proof(self.identity, SECRET, NONCE, ready=True)
        changes = {
            "authority_id": OTHER,
            "instance_id": OTHER,
            "endpoint": "http://127.0.0.1:9999/mcp",
            "ready": False,
            "nonce": "b2" * 32,
            "schema_version": 2,
            "mac": "0" * 64,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(proof)
                changed[field] = value
                with self.assertRaises(IdentityError):
                    verify_proof(self.identity, SECRET, NONCE, changed)
        with self.assertRaises(IdentityError):
            verify_proof(self.identity, "another-owned-secret", NONCE, proof)

    def test_proof_cannot_be_replayed_for_a_fresh_nonce(self):
        proof = make_proof(self.identity, SECRET, NONCE, ready=True)
        with self.assertRaises(IdentityError):
            verify_proof(self.identity, SECRET, "f0" * 32, proof)

    def test_unknown_or_missing_fields_and_boolean_version_are_rejected(self):
        proof = make_proof(self.identity, SECRET, NONCE, ready=True)
        missing = {key: value for key, value in proof.items() if key != "mac"}
        invalid = [
            None, [], {}, missing, {**proof, "extra": "untrusted"},
            {**proof, "schema_version": True}, {**proof, "ready": 1},
            {**proof, "mac": None},
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(IdentityError):
                    verify_proof(self.identity, SECRET, NONCE, value)

    def test_bearer_is_scoped_to_authority_instance_endpoint_and_secret(self):
        baseline = instance_bearer(self.identity, SECRET)
        tokens = {
            baseline,
            instance_bearer(InstanceIdentity(OTHER, INSTANCE, ENDPOINT), SECRET),
            instance_bearer(InstanceIdentity(AUTHORITY, OTHER, ENDPOINT), SECRET),
            instance_bearer(
                InstanceIdentity(AUTHORITY, INSTANCE, "http://127.0.0.1:9999/mcp"),
                SECRET,
            ),
            instance_bearer(self.identity, "another-owned-secret"),
        }
        self.assertEqual(len(tokens), 5)
        self.assertRegex(baseline, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(instance_bearer(self.identity, SECRET), baseline)
        self.assertNotEqual(make_proof(self.identity, SECRET, NONCE, ready=True)["mac"],
                            baseline)

    def test_identity_requires_exact_numeric_loopback_mcp_endpoint(self):
        invalid = [
            "http://localhost:8766/mcp", "http://192.0.2.1:8766/mcp",
            "http://127.0.0.1:8766/mcp/", "http://127.0.0.1:8766/%6dcp",
            "http://127.0.0.1:8766/mcp?x=1", "http://127.0.0.1:8766/mcp#fragment",
            "http://name@127.0.0.1:8766/mcp", "http://127.0.0.1:0/mcp",
            "http://127.0.0.1/mcp", "file:///mcp",
        ]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(IdentityError):
                    InstanceIdentity(AUTHORITY, INSTANCE, endpoint)
        self.assertEqual(
            InstanceIdentity(AUTHORITY, INSTANCE, "http://[::1]:8766/mcp").endpoint,
            "http://[::1]:8766/mcp",
        )

    def test_identity_and_nonce_validation_cannot_leak_input(self):
        for authority, instance in (("bad", INSTANCE), (AUTHORITY, "bad")):
            with self.assertRaises(IdentityError):
                InstanceIdentity(authority, instance, ENDPOINT)
        for nonce in ("", "ab", "z" * 64, None):
            with self.subTest(nonce=nonce):
                with self.assertRaises(IdentityError):
                    make_proof(self.identity, SECRET, nonce, ready=True)
        with self.assertRaises(IdentityError):
            make_proof(self.identity, SECRET, NONCE, ready=1)
        for secret in ("", "sensitive secret with spaces", "bad\nheader"):
            with self.subTest(secret=secret):
                with self.assertRaises(IdentityError) as error:
                    make_proof(self.identity, secret, NONCE, ready=True)
                if secret:
                    self.assertNotIn(secret, str(error.exception))


if __name__ == "__main__":
    unittest.main()
