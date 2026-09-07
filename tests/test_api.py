"""Routing, request handling and the operator endpoints."""

from __future__ import annotations

import json
import unittest

from tests.support import HubTestCase, PROVISIONING_KEY
from iot_hub.api import Request
from iot_hub.auth import HEADER_PROVISIONING


class RoutingTests(HubTestCase):
    def test_unknown_path_is_404(self):
        status, body = self.call("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["status"], 404)

    def test_wrong_method_is_405(self):
        self.assertEqual(self.call("DELETE", "/healthz")[0], 405)

    def test_malformed_json_is_400(self):
        vehicle_id, secret = self.enroll()
        from iot_hub.auth import HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP, HEADER_VEHICLE, sign
        import time

        body = b"{not json"
        timestamp, nonce = str(int(time.time())), "nonce-badjson"
        headers = {
            HEADER_VEHICLE: vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(secret, "POST", "/v1/telemetry", timestamp, nonce, body),
        }
        response = self.api.handle(Request("POST", "/v1/telemetry", headers, body))
        self.assertEqual(response.status, 400)

    def test_missing_points_is_400(self):
        vehicle_id, secret = self.enroll()
        status, body = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry", {})
        self.assertEqual(status, 400)
        self.assertIn("points is required", body["error"]["message"])

    def test_metrics_are_labelled_by_route_template_not_by_vehicle(self):
        """Otherwise metric cardinality would grow with the fleet."""
        for index in range(1, 4):
            self.enroll(f"av-{index:03d}")
            self.operator("GET", f"/v1/vehicles/av-{index:03d}")
        rendered = self.hub.metrics.render()
        self.assertNotIn("av-001", rendered)
        self.assertIn('route="/v1/vehicles/{vehicle_id}"', rendered)

    def test_handler_errors_become_500_without_leaking_internals(self):
        def boom(self_, request):
            raise RuntimeError("secret internal detail")

        self.api.routes[0].handler = boom
        with self.assertLogs("iot_hub.api", level="ERROR"):   # the traceback belongs in the log, not the response
            status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["message"], "internal error")
        self.assertNotIn("secret internal detail", json.dumps(body))


class VehicleAdminTests(HubTestCase):
    def test_enroll_returns_the_secret_exactly_once(self):
        status, created = self.operator("POST", "/v1/vehicles", {"vehicle_id": "av-001", "name": "Shuttle"})
        self.assertEqual(status, 201)
        self.assertIn("secret", created)
        fetched = self.operator("GET", "/v1/vehicles/av-001")[1]
        self.assertNotIn("secret", fetched, "the hub must never disclose a secret again")

    def test_duplicate_enrollment_is_409(self):
        self.operator("POST", "/v1/vehicles", {"vehicle_id": "av-001"})
        self.assertEqual(self.operator("POST", "/v1/vehicles", {"vehicle_id": "av-001"})[0], 409)

    def test_enrollment_validates_the_id(self):
        self.assertEqual(self.operator("POST", "/v1/vehicles", {"vehicle_id": ""})[0], 400)
        self.assertEqual(self.operator("POST", "/v1/vehicles", {"vehicle_id": "x" * 65})[0], 400)
        self.assertEqual(self.operator("POST", "/v1/vehicles", {"name": "no id"})[0], 400)

    def test_list_vehicles_reports_liveness(self):
        vehicle_id, secret = self.enroll()
        self.enroll("av-002")
        self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry", {"points": [self.point(1)]})
        vehicles = {v["vehicle_id"]: v for v in self.operator("GET", "/v1/vehicles")[1]["vehicles"]}
        self.assertTrue(vehicles["av-001"]["online"])
        self.assertFalse(vehicles["av-002"]["online"])
        self.assertIsNotNone(vehicles["av-001"]["latest"])

    def test_status_transitions(self):
        self.enroll()
        self.assertEqual(self.operator("POST", "/v1/vehicles/av-001/status", {"status": "quarantined"})[0], 200)
        self.assertEqual(self.operator("GET", "/v1/vehicles/av-001")[1]["status"], "quarantined")
        self.assertEqual(self.operator("POST", "/v1/vehicles/av-001/status", {"status": "nonsense"})[0], 400)
        self.assertEqual(self.operator("POST", "/v1/vehicles/av-ghost/status", {"status": "active"})[0], 404)

    def test_telemetry_history_endpoint(self):
        vehicle_id, secret = self.enroll()
        self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                        {"points": [self.point(i) for i in range(1, 11)]})
        points = self.operator("GET", f"/v1/vehicles/{vehicle_id}/telemetry?limit=5")[1]["points"]
        self.assertEqual(len(points), 5)

    def test_unknown_vehicle_endpoints_are_404(self):
        for path in ("/v1/vehicles/av-ghost", "/v1/vehicles/av-ghost/telemetry", "/v1/vehicles/av-ghost/commands"):
            self.assertEqual(self.operator("GET", path)[0], 404, path)


class EnrollmentTests(HubTestCase):
    """Field replacement over the wire, gated by the depot provisioning key."""

    def test_enroll_with_provisioning_key(self):
        status, body = self.call("POST", "/v1/enroll", {"vehicle_id": "av-new"},
                                 {HEADER_PROVISIONING: PROVISIONING_KEY})
        self.assertEqual(status, 201)
        self.assertIn("secret", body)
        # The freshly issued secret must work straight away.
        self.assertEqual(self.as_vehicle("av-new", body["secret"], "GET", "/v1/commands")[0], 200)

    def test_enroll_without_the_key_is_rejected(self):
        self.assertEqual(self.call("POST", "/v1/enroll", {"vehicle_id": "av-new"})[0], 401)
        self.assertEqual(self.call("POST", "/v1/enroll", {"vehicle_id": "av-new"},
                                   {HEADER_PROVISIONING: "wrong"})[0], 401)

    def test_enrollment_can_be_disabled_entirely(self):
        self.config.provisioning_key = ""
        status, body = self.call("POST", "/v1/enroll", {"vehicle_id": "av-new"},
                                 {HEADER_PROVISIONING: PROVISIONING_KEY})
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"]["message"])


class ObservabilityTests(HubTestCase):
    def test_readyz_warns_when_the_roster_is_short(self):
        status, body = self.call("GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertIn("450 vehicle(s) not yet provisioned", body["checks"]["roster"])

    def test_readyz_is_clean_for_a_full_fleet(self):
        self.config.fleet_size = 2
        self.enroll("av-001")
        self.enroll("av-002")
        self.assertEqual(self.call("GET", "/readyz")[1]["checks"]["roster"], "ok")

    def test_metrics_endpoint_is_prometheus_text(self):
        response = self.api.handle(Request("GET", "/metrics", {}, b""))
        self.assertTrue(response.content_type.startswith("text/plain"))
        self.assertIn("hub_uptime_seconds", response.body.decode())

    def test_events_log_records_the_audit_trail(self):
        self.enroll()
        self.operator("POST", "/v1/vehicles/av-001/commands", {"type": "ping"})
        kinds = [e["kind"] for e in self.operator("GET", "/v1/events")[1]["events"]]
        self.assertIn("vehicle.enrolled", kinds)
        self.assertIn("command.enqueued", kinds)

    def test_status_endpoint_summarises_the_hub(self):
        body = self.operator("GET", "/v1/status")[1]
        for key in ("ready", "checks", "commands", "vehicles", "database_bytes", "uptime_seconds"):
            self.assertIn(key, body)

    def test_hub_time_is_available_without_credentials(self):
        """A vehicle whose clock is wrong cannot sign; it must still be able to ask the time."""
        status, body = self.call("GET", "/v1/time")
        self.assertEqual(status, 200)
        self.assertIn("now", body)
        self.assertEqual(body["skew_tolerance_seconds"], self.config.clock_skew_tolerance_seconds)


if __name__ == "__main__":
    unittest.main()
