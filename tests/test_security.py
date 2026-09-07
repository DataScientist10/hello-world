"""Regression tests for the findings from the security review.

Each test here corresponds to a specific vulnerability that was found and
fixed. They exist so the fix cannot silently regress -- a failure in this file
means a real security property has been lost, not merely that behaviour
changed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.agent import HubClient, ResponseAuthError, VehicleAgent     # noqa: E402
from edge.run import build_command_handler                            # noqa: E402
from iot_hub.auth import (                                            # noqa: E402
    HEADER_RESPONSE_SIGNATURE,
    sign_response,
    verify_response,
)
from iot_hub.config import Config                                     # noqa: E402
from tests.support import HubTestCase                                 # noqa: E402


class ResponseSigningTests(HubTestCase):
    """Finding 1: hub -> vehicle responses were unauthenticated."""

    def test_vehicle_responses_carry_a_signature(self):
        vehicle_id, secret = self.enroll()
        from iot_hub.api import Request
        from iot_hub.auth import HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP, HEADER_VEHICLE, sign
        import time

        timestamp, nonce = str(int(time.time())), "nonce-signed-response"
        headers = {
            HEADER_VEHICLE: vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(secret, "GET", "/v1/commands", timestamp, nonce, b""),
        }
        response = self.api.handle(Request("GET", "/v1/commands", headers, b""))
        signature = response.headers.get(HEADER_RESPONSE_SIGNATURE)
        self.assertIsNotNone(signature, "vehicle responses must be signed")
        self.assertTrue(verify_response(secret, nonce, response.status, response.body, signature))

    def test_signature_is_bound_to_the_request_nonce(self):
        """Otherwise a genuine old response could be replayed against a new poll."""
        secret = "a" * 64
        body = b'{"commands":[]}'
        signature = sign_response(secret, "nonce-one", 200, body)
        self.assertFalse(verify_response(secret, "nonce-two", 200, body, signature))

    def test_signature_covers_the_body(self):
        secret = "a" * 64
        signature = sign_response(secret, "n", 200, b'{"commands":[]}')
        self.assertFalse(verify_response(secret, "n", 200, b'{"commands":[{"type":"pull_over"}]}', signature))

    def test_operator_responses_are_not_signed(self):
        """Operators hold no per-vehicle secret, so there is nothing to sign with."""
        self.enroll()
        from iot_hub.api import Request
        from iot_hub.auth import HEADER_OPERATOR
        from tests.support import OPERATOR_KEY

        response = self.api.handle(Request("GET", "/v1/fleet", {HEADER_OPERATOR: OPERATOR_KEY}, b""))
        self.assertEqual(response.status, 200)
        self.assertNotIn(HEADER_RESPONSE_SIGNATURE, response.headers)


class RogueHubTests(unittest.TestCase):
    """Finding 1, end to end: an impersonated hub must not be able to command a vehicle."""

    def setUp(self):
        self.responses: list[tuple[int, bytes, dict]] = []

        test_case = self

        class Rogue(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body, extra = test_case.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Rogue)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.secret = "a" * 64

    def test_unsigned_command_response_is_rejected(self):
        """The core attack: answer a long poll without holding any secret."""
        body = json.dumps({"commands": [
            {"command_id": "11111111-1111-4111-8111-111111111111", "type": "pull_over", "payload": {}}
        ]}).encode()
        self.responses.append((200, body, {}))
        client = HubClient(self.url, "av-001", self.secret)
        with self.assertRaises(ResponseAuthError):
            client.request("GET", "/v1/commands?wait=0")

    def test_wrongly_signed_response_is_rejected(self):
        body = json.dumps({"commands": []}).encode()
        # Attacker signs with a secret they control rather than the vehicle's.
        self.responses.append((200, body, {HEADER_RESPONSE_SIGNATURE: sign_response("b" * 64, "x", 200, body)}))
        client = HubClient(self.url, "av-001", self.secret)
        with self.assertRaises(ResponseAuthError):
            client.request("GET", "/v1/commands?wait=0")


class CommandValidationTests(unittest.TestCase):
    """Finding 1: the vehicle must re-validate commands, not trust the wire."""

    def test_unknown_command_type_is_refused(self):
        self.assertFalse(VehicleAgent._is_acceptable(
            {"command_id": "11111111-1111-4111-8111-111111111111", "type": "self_destruct"}))

    def test_out_of_range_payload_is_refused(self):
        self.assertFalse(VehicleAgent._is_acceptable({
            "command_id": "11111111-1111-4111-8111-111111111111",
            "type": "set_speed_limit", "payload": {"limit_mps": 500},
        }))

    def test_missing_id_is_refused(self):
        self.assertFalse(VehicleAgent._is_acceptable({"type": "ping"}))

    def test_valid_command_is_accepted(self):
        self.assertTrue(VehicleAgent._is_acceptable({
            "command_id": "11111111-1111-4111-8111-111111111111",
            "type": "set_speed_limit", "payload": {"limit_mps": 12},
        }))


class CommandDropTraversalTests(unittest.TestCase):
    """Finding 2: command_id was used verbatim as a filename."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cmd-drop-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.command_dir = self.tmpdir / "run" / "av" / "commands"
        (self.tmpdir / "etc").mkdir(parents=True, exist_ok=True)
        self.handler = build_command_handler(self.command_dir)

    def _written_outside(self) -> list[Path]:
        return [p for p in self.tmpdir.rglob("*") if p.is_file() and self.command_dir not in p.parents]

    def test_relative_traversal_is_refused(self):
        with self.assertLogs("edge.run", level="ERROR"):   # the refusal belongs in the log
            result = self.handler({"command_id": "../../../etc/owned", "type": "ping", "payload": {}})
        self.assertFalse(result["ok"])
        self.assertEqual(self._written_outside(), [])

    def test_absolute_path_is_refused(self):
        target = str(self.tmpdir / "etc" / "absolute")
        with self.assertLogs("edge.run", level="ERROR"):
            result = self.handler({"command_id": target, "type": "ping", "payload": {}})
        self.assertFalse(result["ok"])
        self.assertEqual(self._written_outside(), [])

    def test_non_uuid_id_is_refused(self):
        with self.assertLogs("edge.run", level="ERROR"):
            self.assertFalse(self.handler({"command_id": "not-a-uuid", "type": "ping"})["ok"])

    def test_valid_uuid_is_written_inside_the_command_dir(self):
        command_id = "11111111-1111-4111-8111-111111111111"
        self.assertTrue(self.handler({"command_id": command_id, "type": "ping", "payload": {}})["ok"])
        written = self.command_dir / f"{command_id}.json"
        self.assertTrue(written.exists())
        self.assertEqual(json.loads(written.read_text())["type"], "ping")

    def test_repeated_command_is_idempotent(self):
        command = {"command_id": "22222222-2222-4222-8222-222222222222", "type": "ping", "payload": {}}
        self.assertTrue(self.handler(command)["ok"])
        self.assertEqual(self.handler(command).get("note"), "already applied")

    def test_no_temp_files_are_left_behind(self):
        self.handler({"command_id": "33333333-3333-4333-8333-333333333333", "type": "ping", "payload": {}})
        self.assertEqual(list(self.command_dir.glob("*.tmp")), [])


class OperatorChannelTests(unittest.TestCase):
    """Finding 3: a privileged bearer token must not go out in cleartext by default."""

    def test_operator_key_without_tls_is_refused(self):
        config = Config(operator_key="secret")
        with self.assertRaises(ValueError) as caught:
            config.check_operator_channel()
        self.assertIn("cleartext", str(caught.exception))

    def test_provisioning_key_without_tls_is_refused(self):
        with self.assertRaises(ValueError):
            Config(provisioning_key="secret").check_operator_channel()

    def test_tls_satisfies_the_requirement(self):
        Config(operator_key="secret", tls_cert="/tmp/c", tls_key="/tmp/k").check_operator_channel()

    def test_explicit_opt_out_is_honoured(self):
        Config(operator_key="secret", allow_insecure_operator_api=True).check_operator_channel()

    def test_no_keys_means_nothing_to_protect(self):
        Config().check_operator_channel()


class CaPinningTests(unittest.TestCase):
    """Finding 1 follow-on: the documented CA pinning must actually exist."""

    def test_agent_accepts_a_ca_bundle(self):
        client = HubClient("https://hub", "av-001", "a" * 64, ca_cert="/etc/ssl/certs/ca-certificates.crt")
        self.assertIsNotNone(client.ssl_context)

    def test_missing_ca_bundle_does_not_fall_back_to_system_trust(self):
        """Silently trusting the system store would trust the wrong roots."""
        with self.assertRaises(ValueError):
            HubClient("https://hub", "av-001", "a" * 64, ca_cert="/nonexistent/ca.crt")

    def test_no_ca_means_no_context(self):
        self.assertIsNone(HubClient("http://hub", "av-001", "a" * 64).ssl_context)


if __name__ == "__main__":
    unittest.main()
