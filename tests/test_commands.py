"""Command dispatch: long-poll, leasing, redelivery, acks, broadcast."""

from __future__ import annotations

import threading
import time
import unittest

from tests.support import HubTestCase


class CommandDispatchTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.vehicle_id, self.secret = self.enroll()

    def poll(self, wait: float = 0.0):
        return self.as_vehicle(self.vehicle_id, self.secret, "GET", f"/v1/commands?wait={wait}")

    def test_queued_command_is_delivered(self):
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands",
                      {"type": "pull_over", "payload": {"reason": "roadworks"}})
        status, body = self.poll()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["commands"]), 1)
        self.assertEqual(body["commands"][0]["type"], "pull_over")
        self.assertEqual(body["commands"][0]["payload"], {"reason": "roadworks"})

    def test_empty_poll_returns_no_commands(self):
        self.assertEqual(self.poll()[1]["commands"], [])

    def test_long_poll_returns_as_soon_as_a_command_arrives(self):
        """The whole point of long-poll: downlink latency without a broker."""
        result: list = []

        def poller():
            result.append(self.poll(wait=5.0))

        thread = threading.Thread(target=poller)
        started = time.monotonic()
        thread.start()
        time.sleep(0.2)
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "pull_over"})
        thread.join(timeout=10)
        elapsed = time.monotonic() - started

        self.assertEqual(len(result[0][1]["commands"]), 1)
        self.assertLess(elapsed, 2.0, "the poll should wake on enqueue, not time out")

    def test_long_poll_times_out_empty(self):
        started = time.monotonic()
        status, body = self.poll(wait=0.5)
        self.assertEqual(status, 200)
        self.assertEqual(body["commands"], [])
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_leased_command_is_hidden_from_the_next_poll(self):
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "ping"})
        self.assertEqual(len(self.poll()[1]["commands"]), 1)
        self.assertEqual(self.poll()[1]["commands"], [], "an in-flight command must not be handed out twice")

    def test_unacknowledged_command_is_redelivered(self):
        """The vehicle drove out of range mid-command; we must not lose it."""
        self.config.command_lease_seconds = 0        # expire immediately
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "ping"})
        first = self.poll()[1]["commands"][0]
        self.hub.commands.sweep_expired_leases()
        second = self.poll()[1]["commands"][0]
        self.assertEqual(first["command_id"], second["command_id"])
        self.assertEqual(second["attempts"], 2)

    def test_command_gives_up_after_max_attempts(self):
        self.config.command_lease_seconds = 0
        self.config.command_max_attempts = 2
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "ping"})
        for _ in range(3):
            self.poll()
            self.hub.commands.sweep_expired_leases()
        self.assertEqual(self.poll()[1]["commands"], [])
        commands = self.operator("GET", f"/v1/vehicles/{self.vehicle_id}/commands")[1]["commands"]
        self.assertEqual(commands[0]["state"], "failed")
        self.assertIn("max delivery attempts", commands[0]["result"]["error"])

    def test_ack_closes_the_command(self):
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "pull_over"})
        command = self.poll()[1]["commands"][0]
        status, body = self.as_vehicle(self.vehicle_id, self.secret, "POST",
                                       f"/v1/commands/{command['command_id']}/ack",
                                       {"ok": True, "result": {"stopped_at": "bay 4"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "acked")
        self.assertEqual(body["result"], {"stopped_at": "bay 4"})

    def test_ack_is_idempotent(self):
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "ping"})
        command_id = self.poll()[1]["commands"][0]["command_id"]
        path = f"/v1/commands/{command_id}/ack"
        self.as_vehicle(self.vehicle_id, self.secret, "POST", path, {"ok": True})
        status, body = self.as_vehicle(self.vehicle_id, self.secret, "POST", path, {"ok": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "acked", "a retried ack must not flip a completed command")

    def test_failure_ack_is_recorded(self):
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "clear_fault"})
        command_id = self.poll()[1]["commands"][0]["command_id"]
        body = self.as_vehicle(self.vehicle_id, self.secret, "POST", f"/v1/commands/{command_id}/ack",
                               {"ok": False, "result": {"error": "actuator offline"}})[1]
        self.assertEqual(body["state"], "failed")

    def test_a_vehicle_cannot_ack_another_vehicles_command(self):
        other_id, other_secret = self.enroll("av-002")
        self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", {"type": "ping"})
        command_id = self.poll()[1]["commands"][0]["command_id"]
        status, _ = self.as_vehicle(other_id, other_secret, "POST", f"/v1/commands/{command_id}/ack", {"ok": True})
        self.assertEqual(status, 403)

    def test_commands_are_delivered_in_order(self):
        for index in range(5):
            self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands",
                          {"type": "ping", "payload": {"n": index}})
        commands = self.poll()[1]["commands"]
        self.assertEqual([c["payload"]["n"] for c in commands], [0, 1, 2, 3, 4])

    def test_a_vehicle_only_sees_its_own_commands(self):
        other_id, other_secret = self.enroll("av-002")
        self.operator("POST", f"/v1/vehicles/{other_id}/commands", {"type": "ping"})
        self.assertEqual(self.poll()[1]["commands"], [])
        self.assertEqual(len(self.as_vehicle(other_id, other_secret, "GET", "/v1/commands")[1]["commands"]), 1)


class CommandValidationTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.vehicle_id, self.secret = self.enroll()

    def create(self, payload):
        return self.operator("POST", f"/v1/vehicles/{self.vehicle_id}/commands", payload)

    def test_unknown_command_type_is_rejected(self):
        status, body = self.create({"type": "self_destruct"})
        self.assertEqual(status, 400)
        self.assertIn("type must be one of", body["error"]["message"])

    def test_speed_limit_payload_is_validated(self):
        self.assertEqual(self.create({"type": "set_speed_limit", "payload": {"limit_mps": 13.4}})[0], 201)
        self.assertEqual(self.create({"type": "set_speed_limit", "payload": {}})[0], 400)
        self.assertEqual(self.create({"type": "set_speed_limit", "payload": {"limit_mps": 200}})[0], 400)

    def test_software_update_requires_a_digest(self):
        """No internet means packages are staged locally -- the digest is the only integrity check."""
        self.assertEqual(self.create({"type": "software_update", "payload": {"package": "av-stack-2.4.1"}})[0], 400)
        self.assertEqual(self.create({
            "type": "software_update",
            "payload": {"package": "av-stack-2.4.1", "sha256": "a" * 64},
        })[0], 201)

    def test_geofence_polygon_is_validated(self):
        self.assertEqual(self.create({"type": "set_geofence", "payload": {"polygon": [[51.5, -0.1]]}})[0], 400)
        self.assertEqual(self.create({
            "type": "set_geofence",
            "payload": {"polygon": [[51.5, -0.1], [51.6, -0.1], [51.6, -0.2]]},
        })[0], 201)

    def test_command_for_unknown_vehicle_is_404(self):
        self.assertEqual(self.operator("POST", "/v1/vehicles/av-ghost/commands", {"type": "ping"})[0], 404)


class BroadcastTests(HubTestCase):
    def test_broadcast_reaches_every_active_vehicle(self):
        vehicles = [self.enroll(f"av-{i:03d}") for i in range(1, 26)]
        status, body = self.operator("POST", "/v1/commands/broadcast",
                                     {"type": "pull_over", "payload": {"reason": "depot e-stop"}})
        self.assertEqual(status, 201)
        self.assertEqual(body["issued"], 25)
        for vehicle_id, secret in vehicles:
            commands = self.as_vehicle(vehicle_id, secret, "GET", "/v1/commands")[1]["commands"]
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["type"], "pull_over")

    def test_broadcast_skips_quarantined_vehicles(self):
        self.enroll("av-001")
        self.enroll("av-002")
        self.hub.registry.set_status("av-002", "quarantined")
        body = self.operator("POST", "/v1/commands/broadcast", {"type": "ping"})[1]
        self.assertEqual(body["issued"], 1)

    def test_broadcast_can_target_a_subset(self):
        for index in range(1, 4):
            self.enroll(f"av-{index:03d}")
        body = self.operator("POST", "/v1/commands/broadcast",
                             {"type": "ping", "vehicle_ids": ["av-001", "av-003"]})[1]
        self.assertEqual(body["issued"], 2)

    def test_broadcast_rejects_unknown_vehicles_without_partial_delivery(self):
        self.enroll("av-001")
        status, body = self.operator("POST", "/v1/commands/broadcast",
                                     {"type": "ping", "vehicle_ids": ["av-001", "av-ghost"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["detail"]["unknown"], ["av-ghost"])
        self.assertEqual(self.hub.commands.counts_by_state()["pending"], 0)


if __name__ == "__main__":
    unittest.main()
