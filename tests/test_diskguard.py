"""Disk-pressure handling.

The behaviour under test is asymmetric on purpose: when the disk fills, the hub
sheds telemetry (which every vehicle buffers and retries) and keeps dispatching
commands (which nothing else can do). Most of these tests exist to hold that
line.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iot_hub.config import Config                                    # noqa: E402
from iot_hub.diskguard import CRITICAL, OK, WARNING, DiskGuard       # noqa: E402
from tests.support import HubTestCase                                # noqa: E402

GIB = 1024 ** 3


def guard_at(free_gib: float, total_gib: float = 100.0, **overrides) -> DiskGuard:
    """A guard over a disk of a chosen fullness, without filling a real one."""
    config = Config(**overrides)
    return DiskGuard("/nonexistent", config, usage=lambda _p: (int(free_gib * GIB), int(total_gib * GIB)))


class ThresholdTests(unittest.TestCase):
    def test_healthy_disk_is_ok(self):
        state = guard_at(50).sample()
        self.assertEqual(state.state, OK)
        self.assertFalse(state.shedding)

    def test_low_space_warns_without_shedding(self):
        """A warning tightens purging; it must not start refusing uploads."""
        state = guard_at(15).sample()          # 15% free, below the 20% warn line
        self.assertEqual(state.state, WARNING)
        self.assertFalse(state.shedding, "a warning must not shed telemetry")

    def test_critical_space_sheds(self):
        state = guard_at(3).sample()           # 3 GiB free, below the 5 GiB shed line
        self.assertEqual(state.state, CRITICAL)
        self.assertTrue(state.shedding)

    def test_absolute_floor_catches_a_large_disk(self):
        """0.004% of 10 TB is still only 0.4 GB -- not enough room to delete rows in."""
        guard = guard_at(free_gib=0.4, total_gib=10_000)
        self.assertEqual(guard.sample().state, CRITICAL)

    def test_ratio_alone_does_not_nag_a_large_disk(self):
        """12% of 270 GB is 32 GB -- about five days of headroom, not a warning.

        Found by running against a real volume: a ratio-only threshold fires on
        a perfectly healthy large disk, so the ratio is capped by an absolute
        headroom figure.
        """
        state = guard_at(free_gib=32, total_gib=270).sample()
        self.assertEqual(state.state, OK)
        self.assertLess(state.free_ratio, 0.20, "ratio is genuinely below the warn ratio")

    def test_small_disk_still_warns_on_the_same_free_space(self):
        """The same 3 GiB that is fine on a large array is urgent on a 20 GiB disk."""
        self.assertEqual(guard_at(free_gib=3, total_gib=20).sample().state, WARNING)
        self.assertEqual(guard_at(free_gib=3, total_gib=10_000).sample().state, CRITICAL)

    def test_unreadable_volume_is_treated_as_critical(self):
        def explode(_path):
            raise OSError("volume gone")

        guard = DiskGuard("/nonexistent", Config(), usage=explode)
        state = guard.sample()
        self.assertEqual(state.state, CRITICAL)
        self.assertTrue(state.shedding, "an unmeasurable disk must fail safe, not fail open")

    def test_bytes_to_reclaim_targets_the_warning_line(self):
        guard = guard_at(4, total_gib=100)
        guard.sample()
        # Warn line is min(20% of 100 GiB, 20 GiB) = 20 GiB; 4 GiB free leaves 16.
        self.assertAlmostEqual(guard.bytes_to_reclaim() / GIB, 16.0, places=1)


class HysteresisTests(unittest.TestCase):
    """Recovering at the same line it broke at would flap 200s and 503s at the fleet."""

    def setUp(self):
        self.free = 50.0
        config = Config()
        self.guard = DiskGuard("/nonexistent", config,
                               usage=lambda _p: (int(self.free * GIB), 100 * GIB))

    def test_shedding_persists_through_the_warning_band(self):
        self.free = 3.0
        self.assertTrue(self.guard.sample().shedding)

        # Back above critical but still inside the warning band: keep shedding.
        self.free = 15.0
        state = self.guard.sample()
        self.assertEqual(state.state, WARNING)
        self.assertTrue(state.shedding, "must not resume ingest while still in the warning band")

        # Fully recovered.
        self.free = 40.0
        state = self.guard.sample()
        self.assertEqual(state.state, OK)
        self.assertFalse(state.shedding)

    def test_recovery_clears_shedding(self):
        self.free = 1.0
        self.assertTrue(self.guard.sample().shedding)
        self.free = 80.0
        self.assertFalse(self.guard.sample().shedding)


class SheddingBehaviourTests(HubTestCase):
    """What the fleet actually experiences when the disk is full."""

    def _fill_disk(self, free_gib: float) -> None:
        self.hub.disk._usage = lambda _p: (int(free_gib * GIB), 100 * GIB)
        self.hub.disk.sample()

    def test_telemetry_is_refused_with_503_and_retry_after(self):
        vehicle_id, secret = self.enroll()
        self._fill_disk(2)
        status, body = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                       {"points": [self.point(1)]})
        self.assertEqual(status, 503)
        self.assertIn("buffer locally", body["error"]["message"])

    def test_commands_still_dispatch_while_shedding(self):
        """The whole point: a full disk must not stop the hub stopping a vehicle."""
        vehicle_id, secret = self.enroll()
        self._fill_disk(2)

        status, _ = self.operator("POST", f"/v1/vehicles/{vehicle_id}/commands",
                                  {"type": "pull_over", "payload": {"reason": "disk full"}})
        self.assertEqual(status, 201, "operators must still be able to issue commands")

        status, body = self.as_vehicle(vehicle_id, secret, "GET", "/v1/commands")
        self.assertEqual(status, 200, "vehicles must still receive commands")
        self.assertEqual(body["commands"][0]["type"], "pull_over")

        command_id = body["commands"][0]["command_id"]
        status, _ = self.as_vehicle(vehicle_id, secret, "POST", f"/v1/commands/{command_id}/ack",
                                    {"ok": True})
        self.assertEqual(status, 200, "acks must still close out")

    def test_telemetry_resumes_once_space_returns(self):
        vehicle_id, secret = self.enroll()
        self._fill_disk(2)
        self.assertEqual(self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                         {"points": [self.point(1)]})[0], 503)
        self._fill_disk(60)
        status, body = self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                                       {"points": [self.point(1)]})
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_shedding_is_counted(self):
        vehicle_id, secret = self.enroll()
        self._fill_disk(2)
        self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry", {"points": [self.point(1)]})
        self.assertIn("hub_telemetry_shed_total", self.hub.metrics.snapshot_counters())

    def test_readiness_reports_critical_disk(self):
        self._fill_disk(2)
        status, body = self.call("GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(body["checks"]["disk"]["state"], "critical")
        self.assertIn("shed", body["checks"]["disk_note"])

    def test_readiness_notes_a_warning_without_failing(self):
        self._fill_disk(15)
        status, body = self.call("GET", "/readyz")
        self.assertEqual(status, 200, "a warning is not an outage")
        self.assertEqual(body["checks"]["disk"]["state"], "warning")

    def test_probes_and_operator_views_survive_a_full_disk(self):
        self._fill_disk(1)
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        self.assertEqual(self.call("GET", "/metrics")[0], 200)
        self.assertEqual(self.operator("GET", "/v1/fleet")[0], 200)


class ReliefTests(HubTestCase):
    """The escalation that tries to claw space back before shedding is permanent."""

    def _ingest(self, vehicle_id: str, secret: str, count: int) -> None:
        self.as_vehicle(vehicle_id, secret, "POST", "/v1/telemetry",
                        {"points": [self.point(i) for i in range(1, count + 1)]})

    def test_emergency_purge_removes_oldest_first(self):
        vehicle_id, secret = self.enroll()
        self._ingest(vehicle_id, secret, 40)
        # Age every row well past the retention floor so it is eligible.
        self.hub.storage.execute(
            lambda conn: conn.execute("UPDATE telemetry SET received_at = received_at - 86400"))

        removed = self.hub.storage.purge_oldest_telemetry(10, time.time())
        self.assertEqual(removed, 10)
        remaining = [p["seq"] for p in self.hub.telemetry.history(vehicle_id, limit=100)]
        self.assertEqual(len(remaining), 30)
        self.assertNotIn(1, remaining, "the oldest rows should be the ones removed")

    def test_recent_history_is_protected_from_the_emergency_purge(self):
        """A full disk must not cost an investigation the samples it needs most."""
        vehicle_id, secret = self.enroll()
        self._ingest(vehicle_id, secret, 20)
        floor = time.time() - self.config.disk_min_retention_hours * 3600

        removed = self.hub.storage.purge_oldest_telemetry(1000, floor)
        self.assertEqual(removed, 0, "rows inside the protected window must survive")
        self.assertEqual(len(self.hub.telemetry.history(vehicle_id, limit=100)), 20)

    def test_relief_runs_and_reports_rows_removed(self):
        vehicle_id, secret = self.enroll()
        self._ingest(vehicle_id, secret, 30)
        self.hub.storage.execute(
            lambda conn: conn.execute("UPDATE telemetry SET received_at = received_at - 864000"))
        self.hub.disk._usage = lambda _p: (1 * GIB, 100 * GIB)
        self.hub.disk.sample()

        with self.assertLogs("iot_hub", level="WARNING"):
            removed = self.hub._relieve_disk_pressure()
        self.assertGreater(removed, 0)

    def test_wal_checkpoint_is_safe_to_call(self):
        self.hub.storage.checkpoint_wal()
        self.assertEqual(self.hub.storage.telemetry_row_count(), 0)


if __name__ == "__main__":
    unittest.main()
