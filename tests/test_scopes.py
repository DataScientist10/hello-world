"""The read-only operator scope.

A wall dashboard needs to see the fleet. It does not need to be able to stop
450 vehicles or mint a vehicle's signing secret -- but before this scope
existed, the only key that could read the fleet could also do both. These tests
hold the boundary.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iot_hub.auth import HEADER_OPERATOR, SCOPE_OPERATOR, SCOPE_VIEWER   # noqa: E402
from iot_hub.config import Config                                        # noqa: E402
from tests.support import OPERATOR_KEY, HubTestCase                      # noqa: E402

VIEWER_KEY = "test-viewer-key"


class ScopedKeyTestCase(HubTestCase):
    config_overrides = {"viewer_key": VIEWER_KEY}

    def viewer(self, method: str, path: str, payload=None):
        return self.call(method, path, payload, {HEADER_OPERATOR: VIEWER_KEY})


#: Every read route a dashboard needs, and every write route it must not reach.
READ_ROUTES = [
    "/v1/fleet",
    "/v1/fleet/map",
    "/v1/status",
    "/v1/events",
    "/v1/vehicles",
    "/v1/vehicles/av-001",
    "/v1/vehicles/av-001/telemetry",
    "/v1/vehicles/av-001/commands",
]

WRITE_ROUTES = [
    ("POST", "/v1/vehicles", {"vehicle_id": "av-999"}),
    ("POST", "/v1/vehicles/av-001/status", {"status": "quarantined"}),
    ("POST", "/v1/vehicles/av-001/rotate-secret", {}),
    ("POST", "/v1/vehicles/av-001/commands", {"type": "pull_over"}),
    ("POST", "/v1/commands/broadcast", {"type": "pull_over"}),
]


class ViewerCanReadTests(ScopedKeyTestCase):
    def setUp(self):
        super().setUp()
        self.enroll("av-001")

    def test_viewer_reaches_every_read_route(self):
        for path in READ_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.viewer("GET", path)[0], 200)

    def test_viewer_sees_the_same_fleet_data_as_an_operator(self):
        as_viewer = self.viewer("GET", "/v1/fleet")[1]
        as_operator = self.operator("GET", "/v1/fleet")[1]
        as_viewer.pop("generated_at", None)
        as_operator.pop("generated_at", None)
        self.assertEqual(as_viewer, as_operator, "read-only must not mean degraded")


class ViewerCannotWriteTests(ScopedKeyTestCase):
    def setUp(self):
        super().setUp()
        self.enroll("av-001")

    def test_every_write_route_is_refused(self):
        for method, path, payload in WRITE_ROUTES:
            with self.subTest(path=path):
                status, body = self.viewer(method, path, payload)
                self.assertEqual(status, 403, f"{path} must refuse a read-only key")
                self.assertIn("read-only", body["error"]["message"])

    def test_refusal_explains_what_is_needed(self):
        _, body = self.viewer("POST", "/v1/commands/broadcast", {"type": "pull_over"})
        self.assertIn("full operator key", body["error"]["message"])

    def test_a_refused_broadcast_queues_nothing(self):
        """A 403 must not be a partial success."""
        self.viewer("POST", "/v1/commands/broadcast", {"type": "pull_over"})
        self.assertEqual(self.hub.commands.counts_by_state()["pending"], 0)

    def test_operator_key_still_writes(self):
        self.assertEqual(
            self.operator("POST", "/v1/vehicles/av-001/commands", {"type": "pull_over"})[0], 201)


class SecretExposureTests(ScopedKeyTestCase):
    """The escalation the review found: reads must never hand back a secret."""

    def setUp(self):
        super().setUp()
        self.vehicle_id, self.secret = self.enroll("av-001")

    def test_no_read_route_discloses_a_vehicle_secret(self):
        for path in READ_ROUTES:
            with self.subTest(path=path):
                status, body = self.viewer("GET", path)
                self.assertEqual(status, 200)
                serialised = json.dumps(body)
                self.assertNotIn(self.secret, serialised, f"{path} leaked the vehicle secret")
                self.assertNotIn('"secret"', serialised, f"{path} exposes a secret field")

    def test_no_read_route_discloses_the_operator_key(self):
        for path in READ_ROUTES:
            with self.subTest(path=path):
                serialised = json.dumps(self.viewer("GET", path)[1])
                self.assertNotIn(OPERATOR_KEY, serialised)
                self.assertNotIn(VIEWER_KEY, serialised)

    def test_rotate_secret_is_out_of_reach(self):
        """The clearest escalation path: rotate-secret returns a usable identity."""
        status, body = self.viewer("POST", "/v1/vehicles/av-001/rotate-secret", {})
        self.assertEqual(status, 403)
        self.assertNotIn("secret", json.dumps(body))
        # And the vehicle's existing secret still works, i.e. nothing rotated.
        self.assertEqual(self.as_vehicle(self.vehicle_id, self.secret, "GET", "/v1/commands")[0], 200)


class ScopeResolutionTests(ScopedKeyTestCase):
    def test_scopes_resolve_from_the_key_not_the_header(self):
        auth = self.hub.auth
        self.assertEqual(auth.authenticate_operator({HEADER_OPERATOR: OPERATOR_KEY}), SCOPE_OPERATOR)
        self.assertEqual(
            auth.authenticate_operator({HEADER_OPERATOR: VIEWER_KEY}, require_write=False), SCOPE_VIEWER)

    def test_unknown_key_is_401_not_403(self):
        """Wrong credential is an authentication failure, not a scope failure."""
        self.assertEqual(self.call("GET", "/v1/fleet", None, {HEADER_OPERATOR: "nonsense"})[0], 401)

    def test_missing_key_is_refused(self):
        self.assertEqual(self.call("GET", "/v1/fleet")[0], 401)

    def test_scope_is_recorded_for_the_audit_trail(self):
        self.viewer("GET", "/v1/fleet")
        self.operator("GET", "/v1/fleet")
        counters = self.hub.metrics.snapshot_counters()
        self.assertIn("hub_operator_requests_total{scope=viewer}", counters)
        self.assertIn("hub_operator_requests_total{scope=operator}", counters)


class ConfigGuardTests(unittest.TestCase):
    def test_identical_keys_are_refused(self):
        """Otherwise the 'read-only' key silently carries full scope."""
        config = Config(operator_key="same", viewer_key="same", allow_insecure_operator_api=True)
        with self.assertRaises(ValueError) as caught:
            config.check_operator_channel()
        self.assertIn("identical", str(caught.exception))

    def test_distinct_keys_are_accepted(self):
        Config(operator_key="a", viewer_key="b", allow_insecure_operator_api=True).check_operator_channel()

    def test_viewer_key_alone_still_requires_tls(self):
        """It is a lesser credential, but it is still a bearer token on the wire."""
        with self.assertRaises(ValueError):
            Config(viewer_key="v").check_operator_channel()

    def test_viewer_key_alone_enables_the_read_api(self):
        config = Config(viewer_key="v", allow_insecure_operator_api=True)
        config.check_operator_channel()
        self.assertEqual(config.viewer_key, "v")


class ViewerOnlyDeploymentTests(HubTestCase):
    """A hub configured with only a read-only key: nobody can command it."""

    config_overrides = {"operator_key": "", "viewer_key": VIEWER_KEY}

    def test_reads_work_and_writes_are_refused(self):
        self.enroll("av-001")
        headers = {HEADER_OPERATOR: VIEWER_KEY}
        self.assertEqual(self.call("GET", "/v1/fleet", None, headers)[0], 200)
        self.assertEqual(
            self.call("POST", "/v1/vehicles/av-001/commands", {"type": "pull_over"}, headers)[0], 403)


if __name__ == "__main__":
    unittest.main()
