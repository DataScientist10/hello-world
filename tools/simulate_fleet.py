#!/usr/bin/env python3
"""Drive a full fleet of simulated vehicles against a running hub.

This is the load test that turns "450 vehicles" from an assumption into a
measurement. Each simulated vehicle runs the real :class:`~edge.agent.VehicleAgent`
against the real HTTP API -- same signing, same spool, same long-poll -- so what
it exercises is the shipping code path, not a mock of it.

    # terminal 1
    HUB_OPERATOR_KEY=secret python3 -m iot_hub serve --data-dir ./data

    # terminal 2
    python3 tools/simulate_fleet.py --credentials ./credentials --duration 60

Add --broadcast to measure how long a fleet-wide "pull over" takes to reach
every vehicle and come back acknowledged.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.agent import VehicleAgent          # noqa: E402
from iot_hub.auth import HEADER_OPERATOR     # noqa: E402

#: A depot-sized patch of map for the simulated vehicles to drive around in.
DEPOT_LAT, DEPOT_LON = 51.5074, -0.1278


class SimulatedVehicle:
    """A plausible-looking vehicle: drives a loop, drains its battery, sometimes faults."""

    def __init__(self, vehicle_id: str, index: int) -> None:
        self.vehicle_id = vehicle_id
        self.heading = (index * 37) % 360
        self.lat = DEPOT_LAT + random.uniform(-0.02, 0.02)
        self.lon = DEPOT_LON + random.uniform(-0.02, 0.02)
        self.battery = random.uniform(45, 100)
        self.speed = random.uniform(0, 14)
        self.mode = "autonomous"
        self.command_latencies: list[float] = []

    def sample(self) -> dict:
        self.heading = (self.heading + random.uniform(-8, 8)) % 360
        self.speed = max(0.0, min(16.0, self.speed + random.uniform(-1.5, 1.5)))
        # ~1 degree of latitude is 111 km; convert the metres travelled in a tick.
        distance = self.speed / 111_000
        self.lat += distance * math.cos(math.radians(self.heading))
        self.lon += distance * math.sin(math.radians(self.heading))
        self.battery = max(0.0, self.battery - self.speed * 0.0008)

        faults = []
        if random.random() < 0.001:
            faults.append(random.choice(["lidar_degraded", "tire_pressure_low", "camera_occluded"]))
        if self.battery < 15:
            self.mode = "charging"
        return {
            "ts": time.time(),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "speed_mps": round(self.speed, 2),
            "heading_deg": round(self.heading, 1),
            "battery_soc": round(self.battery, 1),
            "mode": self.mode,
            "faults": faults,
        }

    def handle_command(self, command: dict) -> dict:
        """Act on a command the way a real vehicle would, and time the round trip."""
        issued = command.get("created_at")
        if issued:
            self.command_latencies.append(time.time() - issued)
        kind = command.get("type")
        if kind == "pull_over":
            self.speed, self.mode = 0.0, "parked"
        elif kind == "set_speed_limit":
            self.speed = min(self.speed, command.get("payload", {}).get("limit_mps", self.speed))
        elif kind == "return_to_depot":
            self.heading = (self.heading + 180) % 360
        return {"ok": True, "applied": kind}


def load_credentials(directory: Path, limit: int | None) -> list[dict]:
    files = sorted(p for p in directory.glob("*.json") if p.name != "manifest.json")
    if not files:
        raise SystemExit(f"no credential files in {directory} -- run: python3 -m iot_hub provision --out {directory}")
    if limit:
        files = files[:limit]
    return [json.loads(p.read_text()) for p in files]


def operator_call(url: str, key: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url.rstrip("/") + path, data=data, method=method,
        headers={HEADER_OPERATOR: key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate an autonomous-vehicle fleet against the hub")
    parser.add_argument("--credentials", default="./credentials", help="directory of provisioned credential files")
    parser.add_argument("--url", help="hub URL (default: the hub_url inside each credential file)")
    parser.add_argument("--vehicles", type=int, help="how many to run (default: every credential found)")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds to run (default: 60)")
    parser.add_argument("--sample-interval", type=float, default=1.0, help="seconds between samples (default: 1)")
    parser.add_argument("--upload-interval", type=float, default=5.0, help="seconds between uploads (default: 5)")
    parser.add_argument("--spool-dir", default="/tmp/av-sim-spools")
    parser.add_argument("--operator-key", default="", help="enables the fleet-summary and --broadcast checks")
    parser.add_argument("--broadcast", action="store_true", help="time a fleet-wide pull_over mid-run")
    parser.add_argument("--stagger", type=float, default=5.0,
                        help="seconds over which to start the fleet, mimicking a shift start (default: 5)")
    args = parser.parse_args()

    credentials = load_credentials(Path(args.credentials), args.vehicles)
    spool_dir = Path(args.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    for stale in spool_dir.glob("*.db*"):
        stale.unlink()

    hub_url = args.url or credentials[0].get("hub_url")
    print(f"starting {len(credentials)} simulated vehicle(s) against {hub_url}")

    vehicles, agents = [], []
    for index, credential in enumerate(credentials):
        vehicle = SimulatedVehicle(credential["vehicle_id"], index)
        agent = VehicleAgent(
            hub_url=hub_url,
            vehicle_id=credential["vehicle_id"],
            secret=credential["secret"],
            spool_path=spool_dir / f"{credential['vehicle_id']}.db",
            sampler=vehicle.sample,
            command_handler=vehicle.handle_command,
            sample_interval=args.sample_interval,
            upload_interval=args.upload_interval,
            fw_version="sim-1.0.0",
        )
        vehicles.append(vehicle)
        agents.append(agent)

    started = time.time()
    # Stagger the starts: 450 vehicles powering on in the same millisecond is a
    # thundering herd the real depot would never produce.
    per_vehicle_delay = args.stagger / max(1, len(agents))
    for agent in agents:
        agent.start()
        if per_vehicle_delay:
            time.sleep(per_vehicle_delay)
    print(f"all vehicles up after {time.time() - started:.1f}s")

    broadcast_at = None
    if args.broadcast and args.operator_key:
        threading.Timer(
            args.duration / 2,
            lambda: print("broadcast pull_over ->",
                          operator_call(hub_url, args.operator_key, "POST", "/v1/commands/broadcast",
                                        {"type": "pull_over", "payload": {"reason": "simulation"}})[1].get("issued")),
        ).start()
        broadcast_at = args.duration / 2

    deadline = started + args.duration
    while time.time() < deadline:
        time.sleep(min(10.0, max(0.0, deadline - time.time())))
        uploaded = sum(a.stats["uploaded"] for a in agents)
        failures = sum(a.stats["upload_failures"] for a in agents)
        spooled = sum(a.spool.depth() for a in agents)
        elapsed = time.time() - started
        print(f"  t+{elapsed:5.0f}s  uploaded={uploaded:<8} in-spool={spooled:<6} upload-failures={failures}")

    # Two-phase: tell every agent to stop, then collect them. Serial stop()
    # calls would each wait out their own long poll.
    for agent in agents:
        agent.request_stop()
    for agent in agents:
        agent.stop(timeout=0.5)

    elapsed = time.time() - started
    uploaded = sum(a.stats["uploaded"] for a in agents)
    failures = sum(a.stats["upload_failures"] for a in agents)
    executed = sum(a.stats["commands_executed"] for a in agents)
    latencies = [lat for v in vehicles for lat in v.command_latencies]

    print("\n=== simulation summary ===")
    print(f"vehicles              : {len(agents)}")
    print(f"duration              : {elapsed:.1f}s")
    print(f"telemetry points      : {uploaded} ({uploaded / elapsed:.1f}/s)")
    print(f"upload failures       : {failures}")
    print(f"commands executed     : {executed}")
    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"command latency       : p50 {statistics.median(latencies) * 1000:.0f}ms  "
              f"p95 {p95 * 1000:.0f}ms  max {max(latencies) * 1000:.0f}ms")
    if broadcast_at:
        print(f"broadcast issued at   : t+{broadcast_at:.0f}s")

    if args.operator_key:
        status, summary = operator_call(hub_url, args.operator_key, "GET", "/v1/fleet")
        if status == 200:
            print("\n=== hub's view of the fleet ===")
            print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
