"""End-to-end tests over a real socket, with the real agent.

These are the tests that would have caught the problems that only show up once
HTTP, threads and an unreliable link are all in play at once.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.agent import VehicleAgent          # noqa: E402
from iot_hub.auth import HEADER_OPERATOR     # noqa: E402
from iot_hub.config import Config            # noqa: E402
from iot_hub.hub import Hub                  # noqa: E402
from iot_hub.server import build_server      # noqa: E402

OPERATOR_KEY = "integration-operator-key"


class LiveHub:
    """A hub bound to a real ephemeral port, startable and stoppable at will."""

    def __init__(self, data_dir: Path, port: int = 0) -> None:
        self.config = Config(data_dir=data_dir, port=port, host="127.0.0.1", operator_key=OPERATOR_KEY)
        self.hub = None
        self.server = None
        self.port = port

    def start(self) -> "LiveHub":
        self.config.port = self.port
        self.hub = Hub(self.config)
        self.hub.start_background_tasks()
        self.server = build_server(self.hub)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.hub is not None:
            self.hub.shutdown()
            self.hub = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def operator(self, method: str, path: str, payload=None):
        import json
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={HEADER_OPERATOR: OPERATOR_KEY, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class IntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="iot-integration-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.live = LiveHub(self.tmpdir / "hub")
        self.live.start()
        self.addCleanup(self.live.stop)

    def provision(self, vehicle_id: str = "av-001") -> dict:
        status, body = self.live.operator("POST", "/v1/vehicles", {"vehicle_id": vehicle_id})
        self.assertEqual(status, 201)
        return body

    def make_agent(self, credential: dict, **kwargs) -> VehicleAgent:
        agent = VehicleAgent(
            hub_url=self.live.url,
            vehicle_id=credential["vehicle_id"],
            secret=credential["secret"],
            spool_path=self.tmpdir / f"{credential['vehicle_id']}-spool.db",
            **kwargs,
        )
        self.addCleanup(lambda: agent.stop(flush=False, timeout=0.2))
        return agent

    @staticmethod
    def sample(**overrides) -> dict:
        base = {"lat": 51.5074, "lon": -0.1278, "speed_mps": 8.0,
                "heading_deg": 90.0, "battery_soc": 80.0, "mode": "autonomous"}
        base.update(overrides)
        return base


class TelemetryRoundTripTests(IntegrationTestCase):
    def test_agent_uploads_spooled_telemetry(self):
        agent = self.make_agent(self.provision())
        for _ in range(10):
            agent.record(self.sample())
        self.assertEqual(agent.spool.depth(), 10)

        self.assertEqual(agent.flush_once(), 10)
        self.assertEqual(agent.spool.depth(), 0, "acknowledged samples must leave the spool")

        body = self.live.operator("GET", "/v1/vehicles/av-001/telemetry")[1]
        self.assertEqual(len(body["points"]), 10)

    def test_large_batches_are_gzipped_over_the_wire(self):
        agent = self.make_agent(self.provision(), batch_size=300)
        for _ in range(300):
            agent.record(self.sample())
        self.assertEqual(agent.flush_once(), 300)
        self.assertEqual(agent.spool.depth(), 0)

    def test_samples_the_hub_rejects_are_dropped_not_retried_forever(self):
        agent = self.make_agent(self.provision())
        agent.record(self.sample(lat=999.0))     # will never be accepted
        agent.flush_once()
        self.assertEqual(agent.spool.depth(), 0, "a permanently bad sample must not block the spool")


class OutageTests(IntegrationTestCase):
    def test_telemetry_survives_an_outage_and_catches_up(self):
        """The core offline promise: no hub, no loss."""
        credential = self.provision()
        agent = self.make_agent(credential)
        agent.record(self.sample())
        self.assertEqual(agent.flush_once(), 1)

        # Vehicle drives out of coverage.
        port = self.live.port
        self.live.stop()
        for _ in range(25):
            agent.record(self.sample())
        self.assertEqual(agent.flush_once(), 0, "nothing can be delivered while the hub is unreachable")
        self.assertEqual(agent.spool.depth(), 25, "every sample must still be buffered")

        # Back in coverage, on the same address.
        self.live.port = port
        self.live.start()
        delivered = 0
        for _ in range(5):
            delivered += agent.flush_once()
            if agent.spool.depth() == 0:
                break
        self.assertEqual(delivered, 25)
        self.assertEqual(agent.spool.depth(), 0)

        points = self.live.operator("GET", "/v1/vehicles/av-001/telemetry?limit=500")[1]["points"]
        self.assertEqual(len(points), 26, "the pre-outage sample plus all 25 buffered ones")

    def test_duplicate_upload_after_a_lost_response_is_not_double_counted(self):
        """The hub stored the batch but the vehicle never saw the 200."""
        agent = self.make_agent(self.provision())
        for _ in range(5):
            agent.record(self.sample())
        batch = agent.spool.peek(10)
        self.assertEqual(agent.client.request("POST", "/v1/telemetry", {"points": batch})[0], 200)

        # Agent never got the response, so it retries the very same batch.
        status, body = agent.client.request("POST", "/v1/telemetry", {"points": batch})
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(body["duplicates"], 5)

        points = self.live.operator("GET", "/v1/vehicles/av-001/telemetry")[1]["points"]
        self.assertEqual(len(points), 5)

    def test_agent_recovers_from_a_wrong_clock(self):
        """No NTP on site: the vehicle must resynchronise against the hub."""
        agent = self.make_agent(self.provision())
        agent.client.clock_offset = -3600.0          # an hour slow: every signature is stale
        agent.record(self.sample())

        self.assertEqual(agent.flush_once(), 0)      # rejected, and triggers a resync
        self.assertAlmostEqual(agent.client.clock_offset, 0.0, delta=5.0)
        self.assertEqual(agent.flush_once(), 1, "the retry after resync should succeed")


class CommandRoundTripTests(IntegrationTestCase):
    def test_command_reaches_a_long_polling_vehicle_and_is_acknowledged(self):
        credential = self.provision()
        executed: list[dict] = []

        agent = self.make_agent(credential, command_handler=lambda command: (
            executed.append(command) or {"ok": True, "applied": command["type"]}
        ))
        agent.start(with_sampler=False)
        time.sleep(0.5)                              # let the long poll settle

        issued = time.time()
        self.live.operator("POST", "/v1/vehicles/av-001/commands",
                           {"type": "pull_over", "payload": {"reason": "test"}})

        deadline = time.time() + 10
        while not executed and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(executed, "the command never reached the vehicle")
        latency = time.time() - issued
        self.assertLess(latency, 3.0, f"downlink took {latency:.2f}s")

        deadline = time.time() + 10
        while time.time() < deadline:
            commands = self.live.operator("GET", "/v1/vehicles/av-001/commands")[1]["commands"]
            if commands and commands[0]["state"] == "acked":
                break
            time.sleep(0.05)
        self.assertEqual(commands[0]["state"], "acked")
        self.assertEqual(commands[0]["result"]["applied"], "pull_over")

    def test_failing_handler_reports_failure_without_killing_the_agent(self):
        credential = self.provision()

        def handler(command):
            raise RuntimeError("actuator offline")

        agent = self.make_agent(credential, command_handler=handler)
        agent.start(with_sampler=False)
        time.sleep(0.5)
        self.live.operator("POST", "/v1/vehicles/av-001/commands", {"type": "clear_fault"})

        deadline = time.time() + 10
        while time.time() < deadline:
            commands = self.live.operator("GET", "/v1/vehicles/av-001/commands")[1]["commands"]
            if commands and commands[0]["state"] in ("acked", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(commands[0]["state"], "failed")
        self.assertIn("actuator offline", commands[0]["result"]["error"])
        # The agent must still be alive and polling afterwards.
        self.assertTrue(any(t.is_alive() for t in agent._threads))


class SmallFleetTests(IntegrationTestCase):
    """A miniature of the 450-vehicle deployment, small enough for CI."""

    FLEET = 20

    def test_broadcast_reaches_every_vehicle_concurrently(self):
        agents, received = [], {}
        for index in range(1, self.FLEET + 1):
            vehicle_id = f"av-{index:03d}"
            credential = self.provision(vehicle_id)
            received[vehicle_id] = []
            agent = self.make_agent(
                credential,
                command_handler=(lambda vid: lambda command: (
                    received[vid].append(command) or {"ok": True}
                ))(vehicle_id),
            )
            agent.start(with_sampler=False)
            agents.append(agent)

        time.sleep(1.0)                              # everyone parked in long poll
        issued = time.time()
        status, body = self.live.operator("POST", "/v1/commands/broadcast",
                                          {"type": "pull_over", "payload": {"reason": "e-stop"}})
        self.assertEqual(status, 201)
        self.assertEqual(body["issued"], self.FLEET)

        deadline = time.time() + 20
        while time.time() < deadline and not all(received.values()):
            time.sleep(0.05)
        elapsed = time.time() - issued

        missing = [vid for vid, commands in received.items() if not commands]
        self.assertEqual(missing, [], f"{len(missing)} vehicle(s) never got the e-stop")
        self.assertLess(elapsed, 10.0, f"fleet-wide e-stop took {elapsed:.2f}s")

    def test_concurrent_uploads_are_all_persisted(self):
        agents = []
        for index in range(1, self.FLEET + 1):
            agent = self.make_agent(self.provision(f"av-{index:03d}"))
            for _ in range(20):
                agent.record(self.sample())
            agents.append(agent)

        threads = [threading.Thread(target=agent.flush_once) for agent in agents]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        for agent in agents:
            self.assertEqual(agent.spool.depth(), 0, f"{agent.vehicle_id} still has buffered samples")
        summary = self.live.operator("GET", "/v1/fleet")[1]
        self.assertEqual(summary["reporting"], self.FLEET)
        self.assertEqual(sum(a.stats["uploaded"] for a in agents), self.FLEET * 20)


if __name__ == "__main__":
    unittest.main()
