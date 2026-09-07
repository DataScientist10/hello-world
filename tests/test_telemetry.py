"""Telemetry ingest: validation, idempotency, gap detection, retention, fleet view."""

from __future__ import annotations

import time
import unittest

from tests.support import HubTestCase


class IngestTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.vehicle_id, self.secret = self.enroll()

    def post(self, points):
        return self.as_vehicle(self.vehicle_id, self.secret, "POST", "/v1/telemetry", {"points": points})

    def test_batch_upload(self):
        status, body = self.post([self.point(i) for i in range(1, 51)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 50)
        self.assertEqual(body["rejected"], 0)

    def test_reupload_is_idempotent(self):
        """A vehicle that never saw our 200 will re-send the same batch."""
        points = [self.point(i) for i in range(1, 11)]
        self.assertEqual(self.post(points)[1]["accepted"], 10)
        second = self.post(points)[1]
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["duplicates"], 10)
        history = self.hub.telemetry.history(self.vehicle_id, limit=1000)
        self.assertEqual(len(history), 10, "duplicates must not be stored twice")

    def test_one_bad_point_does_not_reject_the_batch(self):
        points = [self.point(1), self.point(2, lat=999.0), self.point(3)]
        status, body = self.post(points)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 2)
        self.assertEqual(body["rejected"], 1)
        self.assertIn("lat", body["errors"][0])

    def test_invalid_mode_is_rejected(self):
        body = self.post([self.point(1, mode="teleport")])[1]
        self.assertEqual(body["rejected"], 1)

    def test_sequence_gap_is_reported(self):
        self.post([self.point(i) for i in range(1, 6)])
        body = self.post([self.point(i) for i in range(20, 25)])[1]
        self.assertEqual(body["gap"], 14, "samples 6..19 were lost to spool overflow")
        kinds = [e["kind"] for e in self.hub.storage.recent_events(10)]
        self.assertIn("telemetry.gap", kinds)

    def test_out_of_order_batch_does_not_rewind_the_live_view(self):
        self.post([self.point(10, battery_soc=50)])
        self.post([self.point(5, battery_soc=90)])   # a late-arriving older batch
        latest = self.hub.telemetry.latest(self.vehicle_id)
        self.assertEqual(latest.seq, 10)
        self.assertEqual(latest.battery_soc, 50)

    def test_batch_size_limit_is_enforced(self):
        oversized = [self.point(i) for i in range(self.config.max_batch_points + 1)]
        status, body = self.post(oversized)
        self.assertEqual(status, 400)
        self.assertIn("per batch", body["error"]["message"])

    def test_faults_are_recorded_as_events(self):
        self.post([self.point(1, faults=["lidar_degraded"])])
        events = [e for e in self.hub.storage.recent_events(10) if e["kind"] == "vehicle.faults"]
        self.assertEqual(events[0]["detail"]["faults"], ["lidar_degraded"])

    def test_gzip_encoded_body_is_accepted(self):
        import gzip
        import json

        from iot_hub.api import Request
        from iot_hub.auth import HEADER_NONCE, HEADER_SIGNATURE, HEADER_TIMESTAMP, HEADER_VEHICLE, sign

        payload = {"points": [self.point(i) for i in range(1, 101)]}
        body = gzip.compress(json.dumps(payload).encode())
        timestamp, nonce = str(int(time.time())), "nonce-gzip"
        headers = {
            HEADER_VEHICLE: self.vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            # Signed over the compressed bytes: what goes on the wire is what is signed.
            HEADER_SIGNATURE: sign(self.secret, "POST", "/v1/telemetry", timestamp, nonce, body),
            "Content-Encoding": "gzip",
        }
        response = self.api.handle(Request("POST", "/v1/telemetry", headers, body))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["accepted"], 100)

    def test_firmware_version_is_tracked(self):
        self.as_vehicle(self.vehicle_id, self.secret, "POST", "/v1/telemetry",
                        {"points": [self.point(1)], "fw_version": "2.4.1"})
        self.assertEqual(self.hub.registry.get(self.vehicle_id).fw_version, "2.4.1")


class FleetViewTests(HubTestCase):
    def test_summary_counts_modes_and_flags_problems(self):
        for index, (mode, battery, faults) in enumerate([
            ("autonomous", 90.0, []),
            ("autonomous", 12.0, []),
            ("charging", 30.0, ["tire_pressure_low"]),
        ], start=1):
            vehicle_id, secret = self.enroll(f"av-{index:03d}")
            self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                            {"points": [self.point(1, mode=mode, battery_soc=battery, faults=faults)]})

        summary = self.operator("GET", "/v1/fleet")[1]
        self.assertEqual(summary["fleet_size"], 3)
        self.assertEqual(summary["reporting"], 3)
        self.assertEqual(summary["modes"], {"autonomous": 2, "charging": 1})
        self.assertEqual(summary["low_battery_vehicles"], ["av-002"])
        self.assertEqual(summary["faulted_vehicles"], ["av-003"])

    def test_live_view_survives_a_hub_restart(self):
        vehicle_id, secret = self.enroll()
        self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                        {"points": [self.point(7, battery_soc=42.0)]})
        self.hub.shutdown()

        from iot_hub.hub import Hub
        restarted = Hub(self.config)
        self.addCleanup(restarted.shutdown)
        latest = restarted.telemetry.latest(vehicle_id)
        self.assertIsNotNone(latest, "the fleet map must be warm again after a restart")
        self.assertEqual(latest.seq, 7)
        self.assertEqual(latest.battery_soc, 42.0)


class RetentionTests(HubTestCase):
    def test_old_telemetry_is_purged(self):
        vehicle_id, secret = self.enroll()
        self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry", {"points": [self.point(1)]})
        self.assertEqual(len(self.hub.telemetry.history(vehicle_id)), 1)
        removed = self.hub.storage.purge_telemetry(older_than=time.time() + 1)
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.hub.telemetry.history(vehicle_id)), 0)


if __name__ == "__main__":
    unittest.main()
