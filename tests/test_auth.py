"""Authentication: signing, replay protection, clock skew, isolation between vehicles."""

from __future__ import annotations

import time
import unittest

from tests.support import HubTestCase
from iot_hub.auth import HEADER_OPERATOR, NonceCache, sign


class SigningTests(HubTestCase):
    def test_valid_signature_is_accepted(self):
        vehicle_id, secret = self.enroll()
        status, body = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                       {"points": [self.point(1)]})
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_wrong_secret_is_rejected(self):
        vehicle_id, _ = self.enroll()
        status, body = self.as_vehicle(vehicle_id, "0" * 64, "POST", "/v1/telemetry",
                                       {"points": [self.point(1)]})
        self.assertEqual(status, 401)
        self.assertIn("bad signature", body["error"]["message"])

    def test_unknown_vehicle_is_rejected(self):
        status, _ = self.as_vehicle("av-ghost", "0" * 64, "POST", "/v1/telemetry", {"points": []})
        self.assertEqual(status, 401)

    def test_tampering_with_the_body_breaks_the_signature(self):
        vehicle_id, secret = self.enroll()
        payload = {"points": [self.point(1)]}
        timestamp, nonce = str(int(time.time())), "nonce-tamper"
        import json

        from iot_hub.api import Request
        from iot_hub.auth import HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP, HEADER_VEHICLE

        original = json.dumps(payload).encode()
        headers = {
            HEADER_VEHICLE: vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(secret, "POST", "/v1/telemetry", timestamp, nonce, original),
        }
        # Same signature, different body: an attacker on the depot LAN editing
        # a vehicle's reported position in flight.
        tampered = json.dumps({"points": [self.point(1, lat=0.0)]}).encode()
        response = self.api.handle(Request("POST", "/v1/telemetry", headers, tampered))
        self.assertEqual(response.status, 401)

    def test_tampering_with_the_query_string_breaks_the_signature(self):
        vehicle_id, secret = self.enroll()
        timestamp, nonce = str(int(time.time())), "nonce-query"
        from iot_hub.api import Request
        from iot_hub.auth import HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP, HEADER_VEHICLE

        headers = {
            HEADER_VEHICLE: vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(secret, "GET", "/v1/commands?wait=0", timestamp, nonce, b""),
        }
        response = self.api.handle(Request("GET", "/v1/commands?wait=25", headers, b""))
        self.assertEqual(response.status, 401)

    def test_replayed_request_is_rejected(self):
        vehicle_id, secret = self.enroll()
        timestamp, nonce = str(int(time.time())), "nonce-replay"
        first = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                {"points": [self.point(1)]}, timestamp=timestamp, nonce=nonce)
        second = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                 {"points": [self.point(1)]}, timestamp=timestamp, nonce=nonce)
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 401)
        self.assertIn("replayed", second[1]["error"]["message"])

    def test_stale_timestamp_is_rejected_and_hub_time_is_returned(self):
        vehicle_id, secret = self.enroll()
        stale = str(int(time.time()) - self.config.clock_skew_tolerance_seconds - 60)
        status, body = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                       {"points": [self.point(1)]}, timestamp=stale)
        self.assertEqual(status, 401)
        # The vehicle has no NTP; the hub tells it what time it is so it can recover.
        self.assertIn("hub_time", body["error"])

    def test_quarantined_vehicle_is_locked_out(self):
        vehicle_id, secret = self.enroll()
        self.hub.registry.set_status(vehicle_id, "quarantined")
        status, body = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                       {"points": [self.point(1)]})
        self.assertEqual(status, 403)
        self.assertIn("quarantined", body["error"]["message"])

    def test_secret_rotation_invalidates_the_old_secret(self):
        vehicle_id, old_secret = self.enroll()
        new_secret = self.hub.registry.rotate_secret(vehicle_id).secret
        self.assertNotEqual(old_secret, new_secret)
        self.assertEqual(self.as_vehicle(vehicle_id, old_secret, "GET", "/v1/commands")[0], 401)
        self.assertEqual(self.as_vehicle(vehicle_id, new_secret, "GET", "/v1/commands")[0], 200)


class OperatorAuthTests(HubTestCase):
    def test_operator_key_required(self):
        self.assertEqual(self.call("GET", "/v1/fleet")[0], 401)
        self.assertEqual(self.call("GET", "/v1/fleet", None, {HEADER_OPERATOR: "wrong"})[0], 401)
        self.assertEqual(self.operator("GET", "/v1/fleet")[0], 200)

    def test_vehicle_cannot_reach_the_operator_api(self):
        vehicle_id, secret = self.enroll()
        status, _ = self.as_vehicle(vehicle_id, secret, "GET", "/v1/fleet")
        self.assertEqual(status, 401)

    def test_probes_need_no_credentials(self):
        for path in ("/healthz", "/readyz", "/metrics", "/v1/time"):
            self.assertEqual(self.call("GET", path)[0], 200, path)


class NonceCacheTests(unittest.TestCase):
    def test_nonces_expire_with_the_skew_window(self):
        cache = NonceCache(ttl_seconds=100)
        now = 1000.0
        self.assertTrue(cache.check_and_add("av-1", "n1", now))
        self.assertFalse(cache.check_and_add("av-1", "n1", now))
        # Same nonce from a different vehicle is a different key.
        self.assertTrue(cache.check_and_add("av-2", "n1", now))
        # Once the window has passed the entry is pruned, and by then the
        # timestamp check is what rejects the replay.
        self.assertTrue(cache.check_and_add("av-1", "n1", now + 500))


if __name__ == "__main__":
    unittest.main()
