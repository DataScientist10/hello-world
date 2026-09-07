"""Shared helpers for the test suite."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iot_hub.api import Api, Request          # noqa: E402
from iot_hub.auth import (                    # noqa: E402
    HEADER_NONCE,
    HEADER_OPERATOR,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_VEHICLE,
    sign,
)
from iot_hub.config import Config             # noqa: E402
from iot_hub.hub import Hub                   # noqa: E402

OPERATOR_KEY = "test-operator-key"
PROVISIONING_KEY = "test-provisioning-key"


class HubTestCase(unittest.TestCase):
    """A hub on a throwaway database, driven through the real API router."""

    #: Subclasses may override before setUp runs.
    config_overrides: dict = {}

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="iot-hub-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # Defaults first, then overrides, so a subclass can replace any of them
        # (including clearing a key) rather than colliding with it.
        settings = {
            "data_dir": self.tmpdir,
            "operator_key": OPERATOR_KEY,
            "provisioning_key": PROVISIONING_KEY,
        }
        settings.update(self.config_overrides)
        self.config = Config(**settings)
        self.hub = Hub(self.config)
        self.addCleanup(self.hub.shutdown)
        self.api = Api(self.hub)

    # -- request helpers ----------------------------------------------------
    def call(self, method: str, path: str, payload=None, headers: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else b""
        request = Request(method=method, raw_path=path, headers=headers or {}, body=body)
        response = self.api.handle(request)
        parsed = json.loads(response.body) if response.body and response.content_type.startswith("application/json") else response.body
        return response.status, parsed

    def operator(self, method: str, path: str, payload=None):
        return self.call(method, path, payload, {HEADER_OPERATOR: OPERATOR_KEY})

    def as_vehicle(self, vehicle_id: str, secret: str, method: str, path: str, payload=None,
                   timestamp: str | None = None, nonce: str | None = None):
        body = json.dumps(payload).encode() if payload is not None else b""
        timestamp = timestamp or str(int(time.time()))
        nonce = nonce or f"nonce-{time.time_ns()}"
        headers = {
            HEADER_VEHICLE: vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(secret, method, path, timestamp, nonce, body),
        }
        request = Request(method=method, raw_path=path, headers=headers, body=body)
        response = self.api.handle(request)
        parsed = json.loads(response.body) if response.body else {}
        return response.status, parsed

    # -- fixtures -----------------------------------------------------------
    def enroll(self, vehicle_id: str = "av-001") -> tuple[str, str]:
        vehicle = self.hub.registry.enroll(vehicle_id, name=vehicle_id)
        return vehicle.vehicle_id, vehicle.secret

    @staticmethod
    def point(seq: int, **overrides) -> dict:
        base = {
            "seq": seq,
            "ts": time.time(),
            "lat": 51.5074,
            "lon": -0.1278,
            "speed_mps": 8.0,
            "heading_deg": 90.0,
            "battery_soc": 80.0,
            "mode": "autonomous",
        }
        base.update(overrides)
        return base
