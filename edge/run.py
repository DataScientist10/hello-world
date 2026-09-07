"""Runnable entry point for the on-vehicle agent: ``python3 -m edge.run``.

Reads its credential file and spool location from the environment (see
deploy/av-agent.service). The sampler here reads from a JSON file that the
vehicle's own autonomy stack is expected to write -- replace
:func:`build_sampler` with a real bridge to the vehicle bus for production use.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge.agent import VehicleAgent          # noqa: E402

log = logging.getLogger("edge.run")


def build_sampler(state_path: Path):
    """Read the vehicle's current state, as published by its autonomy stack.

    The contract is deliberately a plain JSON file: it decouples the agent's
    release cycle from the autonomy stack's, and a stale or missing file
    degrades to a 'fault' sample rather than stopping telemetry altogether.
    """

    def sample() -> dict:
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cannot read vehicle state from %s (%s)", state_path, exc)
            return {
                "ts": time.time(), "lat": 0.0, "lon": 0.0, "speed_mps": 0.0,
                "heading_deg": 0.0, "battery_soc": 0.0, "mode": "fault",
                "faults": ["telemetry_source_unavailable"],
            }
        return {
            "ts": state.get("ts", time.time()),
            "lat": state.get("lat", 0.0),
            "lon": state.get("lon", 0.0),
            "speed_mps": state.get("speed_mps", 0.0),
            "heading_deg": state.get("heading_deg", 0.0),
            "battery_soc": state.get("battery_soc", 0.0),
            "mode": state.get("mode", "autonomous"),
            "faults": state.get("faults", []),
            "extra": state.get("extra", {}),
        }

    return sample


def build_command_handler(command_dir: Path):
    """Hand commands to the autonomy stack by writing them into a watched directory.

    Writing to a temporary name and renaming keeps the drop atomic, so the
    autonomy stack never sees a half-written command. The handler is
    idempotent on ``command_id`` because the hub redelivers unacknowledged
    commands by design.
    """
    command_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = command_dir.resolve()

    def handle(command: dict) -> dict:
        # command_id becomes a filename, so it must be proven safe first. The
        # hub only ever issues UUIDs; anything else means the response did not
        # come from the hub. pathlib does not normalise "..", and an absolute
        # segment silently discards the base, so a raw id here would let a
        # forged response write outside the command directory.
        raw_id = command.get("command_id")
        try:
            command_id = str(uuid.UUID(str(raw_id)))
        except (ValueError, AttributeError, TypeError):
            log.error("rejecting command with a non-UUID id: %r", raw_id)
            return {"ok": False, "error": "invalid command_id"}

        target = (command_dir / f"{command_id}.json").resolve()
        if not target.is_relative_to(resolved_dir):      # belt and braces after the UUID check
            log.error("rejecting command whose path escapes %s: %r", resolved_dir, raw_id)
            return {"ok": False, "error": "invalid command_id"}

        if target.exists():
            return {"ok": True, "note": "already applied"}

        # Write via a random temp name in the same directory so a partially
        # written file can never be mistaken for a real command.
        handle_fd, temporary = tempfile.mkstemp(dir=str(resolved_dir), suffix=".tmp")
        try:
            with os.fdopen(handle_fd, "w") as stream:
                json.dump(command, stream, indent=2)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        log.info("accepted command %s (%s)", command_id, command.get("type"))
        return {"ok": True, "accepted_at": time.time()}

    return handle


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("AV_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    credential = os.environ.get("AV_CREDENTIAL", "/etc/av-agent/credential.json")
    if not Path(credential).exists():
        print(f"error: credential file not found at {credential}", file=sys.stderr)
        return 2

    agent = VehicleAgent(
        credential_path=credential,
        spool_path=os.environ.get("AV_SPOOL", "/var/lib/av-agent/spool.db"),
        sampler=build_sampler(Path(os.environ.get("AV_STATE", "/run/av/state.json"))),
        command_handler=build_command_handler(Path(os.environ.get("AV_COMMAND_DIR", "/run/av/commands"))),
        sample_interval=float(os.environ.get("AV_SAMPLE_INTERVAL", "1.0")),
        upload_interval=float(os.environ.get("AV_UPLOAD_INTERVAL", "5.0")),
        max_spool_points=int(os.environ.get("AV_SPOOL_MAX_POINTS", "20000")),
        fw_version=os.environ.get("AV_FW_VERSION", "unknown"),
    )

    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    agent.start()
    log.info("agent running for %s against %s", agent.vehicle_id, agent.client.hub_url)
    try:
        stopped.wait()
    finally:
        log.info("stopping; flushing %d buffered sample(s)", agent.spool.depth())
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
