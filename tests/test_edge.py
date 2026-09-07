"""The vehicle-side agent and its store-and-forward spool."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.agent import _next_backoff        # noqa: E402
from edge.spool import Spool                # noqa: E402


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="spool-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = self.tmpdir / "spool.db"

    def test_append_and_peek_are_fifo(self):
        spool = Spool(self.path)
        self.addCleanup(spool.close)
        for value in range(5):
            spool.append({"ts": value})
        self.assertEqual([p["ts"] for p in spool.peek(10)], [0, 1, 2, 3, 4])

    def test_sequence_numbers_are_assigned_and_monotonic(self):
        spool = Spool(self.path)
        self.addCleanup(spool.close)
        for _ in range(3):
            spool.append({"ts": 0})
        self.assertEqual([p["seq"] for p in spool.peek(10)], [1, 2, 3])

    def test_sequence_counter_survives_a_power_cut(self):
        """A reboot must not restart numbering, or the hub would see duplicates."""
        spool = Spool(self.path)
        spool.append({"ts": 1})
        spool.append({"ts": 2})
        spool.close()

        reopened = Spool(self.path)
        self.addCleanup(reopened.close)
        reopened.append({"ts": 3})
        self.assertEqual([p["seq"] for p in reopened.peek(10)], [1, 2, 3])

    def test_buffered_samples_survive_a_power_cut(self):
        spool = Spool(self.path)
        spool.append({"ts": 1, "lat": 51.5})
        spool.close()
        reopened = Spool(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.depth(), 1)
        self.assertEqual(reopened.peek(1)[0]["lat"], 51.5)

    def test_release_removes_only_acknowledged_samples(self):
        spool = Spool(self.path)
        self.addCleanup(spool.close)
        for value in range(5):
            spool.append({"ts": value})
        spool.release([1, 2])
        self.assertEqual([p["seq"] for p in spool.peek(10)], [3, 4, 5])

    def test_overflow_drops_the_oldest_samples(self):
        """After a long outage, where the vehicle is now beats where it was."""
        spool = Spool(self.path, max_points=10)
        self.addCleanup(spool.close)
        for value in range(25):
            spool.append({"ts": value})
        remaining = spool.peek(50)
        self.assertEqual(spool.depth(), 10)
        self.assertEqual(spool.dropped, 15)
        self.assertEqual([p["seq"] for p in remaining], list(range(16, 26)))


class BackoffTests(unittest.TestCase):
    def test_backoff_grows_but_stays_capped_and_jittered(self):
        delays = []
        current = 1.0
        for _ in range(12):
            current = _next_backoff(current, cap=60.0)
            delays.append(current)
        self.assertTrue(all(0 < d <= 60.0 for d in delays), delays)
        self.assertGreater(max(delays), 10.0, "backoff should actually grow")
        # Jitter matters: 450 vehicles reconnecting in lockstep is a stampede.
        self.assertGreater(len(set(delays)), 6)


if __name__ == "__main__":
    unittest.main()
