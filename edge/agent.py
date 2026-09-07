"""The agent that runs on each autonomous vehicle.

Responsibilities, in the order they matter when things go wrong:

1. **Never lose telemetry.** Every sample is spooled to disk first and released
   only after the hub confirms the write.
2. **Keep the downlink live.** A long-poll thread holds one request open so the
   hub can reach the vehicle within milliseconds without the vehicle needing a
   listening port or a stable address.
3. **Behave on a flaky link.** Exponential backoff with jitter, so 450 vehicles
   coming back into coverage after an outage do not stampede the hub.
4. **Survive a wrong clock.** With no NTP upstream, the vehicle treats the hub
   as its time authority and corrects its own offset rather than getting stuck
   in a signature-rejection loop.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iot_hub.auth import (        # noqa: E402  (path set up above so the agent can ship as one folder)
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_VEHICLE,
    sign,
)
from edge.spool import Spool      # noqa: E402

log = logging.getLogger("edge.agent")

#: Bodies above this are worth compressing on shared depot Wi-Fi.
GZIP_THRESHOLD_BYTES = 1024


class HubClient:
    """Signed HTTP client for one vehicle."""

    def __init__(self, hub_url: str, vehicle_id: str, secret: str, timeout: float = 60.0) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.vehicle_id = vehicle_id
        self.secret = secret
        self.timeout = timeout
        #: hub_time - local_time, applied when signing.
        self.clock_offset = 0.0

    # -- plumbing -----------------------------------------------------------
    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time() + self.clock_offset))
        nonce = secrets.token_hex(12)
        return {
            HEADER_VEHICLE: self.vehicle_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(self.secret, method, path, timestamp, nonce, body),
        }

    def request(self, method: str, path: str, payload: Any = None, timeout: float | None = None) -> tuple[int, Any]:
        body = b""
        headers: dict[str, str] = {"Accept-Encoding": "gzip"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if len(body) > GZIP_THRESHOLD_BYTES:
                body = gzip.compress(body, compresslevel=6)
                headers["Content-Encoding"] = "gzip"
        # Signed after compression: the signature covers the bytes on the wire.
        headers.update(self._headers(method, path, body))

        request = urllib.request.Request(self.hub_url + path, data=body or None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, _decode(response)
        except urllib.error.HTTPError as exc:
            parsed = _decode(exc)
            if exc.code == 401:
                self._maybe_resync_clock(parsed)
            return exc.code, parsed

    def _maybe_resync_clock(self, parsed: Any) -> None:
        """Adopt the hub's clock when it says ours is out of the signing window."""
        hub_time = None
        if isinstance(parsed, dict):
            hub_time = parsed.get("error", {}).get("hub_time")
        if hub_time is None:
            hub_time = self.fetch_hub_time()
        if hub_time is not None:
            self.clock_offset = hub_time - time.time()
            log.warning("clock corrected against hub: offset now %.1fs", self.clock_offset)

    def fetch_hub_time(self) -> float | None:
        """Unauthenticated, because it is what you call *before* you can sign."""
        try:
            with urllib.request.urlopen(self.hub_url + "/v1/time", timeout=10) as response:
                return _decode(response).get("now")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def sync_clock(self) -> bool:
        hub_time = self.fetch_hub_time()
        if hub_time is None:
            return False
        self.clock_offset = hub_time - time.time()
        return True


def _decode(response) -> Any:
    raw = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw[:200].decode("utf-8", "replace")}


class VehicleAgent:
    """Ties the sampler, the spool and the two hub-facing loops together."""

    def __init__(
        self,
        credential_path: str | Path | None = None,
        *,
        hub_url: str | None = None,
        vehicle_id: str | None = None,
        secret: str | None = None,
        spool_path: str | Path = "/var/lib/av-agent/spool.db",
        sampler: Callable[[], dict[str, Any]] | None = None,
        command_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        sample_interval: float = 1.0,
        upload_interval: float = 5.0,
        batch_size: int = 200,
        max_spool_points: int = 20_000,
        fw_version: str = "unknown",
    ) -> None:
        if credential_path is not None:
            credential = json.loads(Path(credential_path).read_text())
            vehicle_id = vehicle_id or credential["vehicle_id"]
            secret = secret or credential["secret"]
            hub_url = hub_url or credential.get("hub_url")
        if not (vehicle_id and secret and hub_url):
            raise ValueError("vehicle_id, secret and hub_url are required (directly or via credential_path)")

        self.client = HubClient(hub_url, vehicle_id, secret)
        self.vehicle_id = vehicle_id
        self.spool = Spool(spool_path, max_points=max_spool_points)
        self.sampler = sampler
        self.command_handler = command_handler or (lambda command: {"handled": False})
        self.sample_interval = sample_interval
        self.upload_interval = upload_interval
        self.batch_size = batch_size
        self.fw_version = fw_version

        self.stats = {"uploaded": 0, "commands_executed": 0, "upload_failures": 0}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ----------------------------------------------------------
    def start(self, with_sampler: bool = True) -> None:
        self.client.sync_clock()
        if with_sampler and self.sampler is not None:
            self._spawn("av-sampler", self._sample_loop)
        self._spawn("av-uploader", self._upload_loop)
        self._spawn("av-commands", self._command_loop)

    def _spawn(self, name: str, target) -> None:
        thread = threading.Thread(target=target, name=f"{name}-{self.vehicle_id}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def request_stop(self) -> None:
        """Signal the loops to exit without waiting for them.

        Shutting a whole fleet down is a two-phase operation: signal everyone
        first, then collect. Doing it one vehicle at a time means every agent
        pays the full join timeout in series.
        """
        self._stop.set()

    def stop(self, flush: bool = True, timeout: float = 2.0) -> None:
        self._stop.set()
        for thread in self._threads:
            # The command thread may be parked in a 25s long poll. It is a
            # daemon thread with no state to lose, so we do not wait it out.
            thread.join(timeout=timeout)
        if flush:
            try:
                self.flush_once()        # last chance to hand over buffered samples
            except Exception:            # noqa: BLE001 - shutting down anyway
                log.debug("final flush failed", exc_info=True)
        self.spool.close()

    # -- telemetry ----------------------------------------------------------
    def record(self, sample: dict[str, Any]) -> None:
        """Buffer one sample (also the entry point for a vehicle's own code)."""
        sample.setdefault("ts", time.time() + self.client.clock_offset)
        self.spool.append(sample)

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.record(self.sampler())
            except Exception:            # noqa: BLE001 - a bad sample must not stop the agent
                log.exception("sampler failed")
            self._stop.wait(self.sample_interval)

    def flush_once(self) -> int:
        """Upload one batch. Returns the number of samples the hub accepted.

        Being out of coverage is the normal state of affairs for a vehicle, not
        an exceptional one, so an unreachable hub returns 0 with everything
        still safely spooled rather than raising at the caller.
        """
        batch = self.spool.peek(self.batch_size)
        if not batch:
            return 0
        try:
            status, body = self.client.request(
                "POST", "/v1/telemetry", {"points": batch, "fw_version": self.fw_version}
            )
        except (urllib.error.URLError, OSError) as exc:
            self.stats["upload_failures"] += 1
            log.info("hub unreachable (%s); %d sample(s) buffered", exc, self.spool.depth())
            return 0
        if status == 200:
            # Duplicates count as delivered: the hub already has them from an
            # earlier attempt whose response we never saw.
            self.spool.release([point["seq"] for point in batch])
            accepted = body.get("accepted", 0)
            self.stats["uploaded"] += accepted
            return accepted
        if status == 400:
            # The hub will never accept this batch. Drop the samples it named as
            # bad rather than retrying them until the spool overflows.
            log.error("hub rejected a batch: %s", body)
            self.spool.release([point["seq"] for point in batch])
            return 0
        self.stats["upload_failures"] += 1
        log.warning("upload failed with status %s: %s", status, body)
        return 0

    def _upload_loop(self) -> None:
        backoff = self.upload_interval
        while not self._stop.is_set():
            try:
                accepted = self.flush_once()
                if accepted or self.spool.depth() == 0:
                    backoff = self.upload_interval
                else:
                    # Nothing got through and samples are still queued: back off
                    # so a fleet-wide outage does not become a fleet-wide retry storm.
                    backoff = _next_backoff(backoff)
            except Exception:            # noqa: BLE001 - the loop must outlive any single failure
                log.exception("upload loop error")
                backoff = _next_backoff(backoff)
            self._stop.wait(backoff)

    # -- commands -----------------------------------------------------------
    def _command_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # wait=25 is the long poll: the hub holds this open until it has
                # work for us, so a "pull over" lands in milliseconds.
                status, body = self.client.request("GET", "/v1/commands?wait=25&max=8", timeout=45)
                if status != 200:
                    log.warning("command poll failed with status %s: %s", status, body)
                    self._stop.wait(_next_backoff(backoff))
                    backoff = _next_backoff(backoff)
                    continue
                backoff = 1.0
                for command in body.get("commands", []):
                    self._execute(command)
            except (urllib.error.URLError, OSError) as exc:
                log.info("command poll unreachable (%s)", exc)
                self._stop.wait(backoff)
                backoff = _next_backoff(backoff)
            except Exception:            # noqa: BLE001
                log.exception("command loop error")
                self._stop.wait(backoff)
                backoff = _next_backoff(backoff)

    def _execute(self, command: dict[str, Any]) -> None:
        command_id = command.get("command_id")
        try:
            result = self.command_handler(command) or {}
            ok = bool(result.pop("ok", True))
        except Exception as exc:         # noqa: BLE001 - report the failure, do not crash the vehicle agent
            log.exception("command %s failed", command_id)
            ok, result = False, {"error": str(exc)}
        self.stats["commands_executed"] += 1
        try:
            self.client.request("POST", f"/v1/commands/{command_id}/ack", {"ok": ok, "result": result})
        except (urllib.error.URLError, OSError):
            # The lease will expire and the hub will redeliver. Handlers are
            # expected to be idempotent on command_id for exactly this reason.
            log.warning("could not acknowledge %s; the hub will redeliver it", command_id)


def _next_backoff(current: float, cap: float = 60.0) -> float:
    """Exponential backoff with jitter, so a fleet-wide outage does not end in a stampede."""
    return min(cap, current * 2) * (0.5 + random.random() / 2)
