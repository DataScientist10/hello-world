"""Wiring: one object that owns every subsystem plus the housekeeping threads."""

from __future__ import annotations

import logging
import threading
import time

from .auth import Authenticator
from .commands import CommandQueue
from .config import Config
from .metrics import Metrics
from .registry import Registry
from .storage import Storage
from .telemetry import TelemetryService

log = logging.getLogger("iot_hub")


class Hub:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.metrics = Metrics()
        self.storage = Storage(self.config.db_path)
        self.registry = Registry(self.storage)
        self.telemetry = TelemetryService(self.storage, self.config, self.registry)
        self.commands = CommandQueue(self.storage, self.config)
        self.auth = Authenticator(self.registry, self.config)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._shutdown_done = False

    # -- lifecycle ----------------------------------------------------------
    def start_background_tasks(self) -> None:
        self._spawn("hub-lease-sweeper", self._lease_sweeper, interval=5.0)
        self._spawn("hub-housekeeper", self._housekeeper, interval=self.config.purge_interval_seconds)
        self._spawn("hub-gauges", self._refresh_gauges, interval=10.0)
        self.storage.log_event("hub.started", detail={"fleet_size": self.registry.count()})

    def _spawn(self, name: str, fn, interval: float) -> None:
        def loop() -> None:
            # Run once up front so gauges are populated before the first scrape.
            while True:
                try:
                    fn()
                except Exception:                       # noqa: BLE001 - a background task must never kill the hub
                    log.exception("background task %s failed", name)
                if self._stop.wait(interval):
                    return

        thread = threading.Thread(target=loop, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def shutdown(self) -> None:
        """Stop cleanly. Safe to call more than once.

        The signal handler and the ``finally`` block in serve() both reach for
        this, and a double shutdown must not raise on the way out.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
        try:
            self.registry.flush_last_seen()
            self.storage.log_event("hub.stopped")
        except Exception:                               # noqa: BLE001 - best effort on the way out
            log.exception("error flushing state during shutdown")
        self.storage.close()

    # -- background work ----------------------------------------------------
    def _lease_sweeper(self) -> None:
        requeued = self.commands.sweep_expired_leases()
        if requeued:
            self.metrics.increment("hub_command_leases_expired_total", requeued)
            log.warning("requeued %d command(s) whose lease expired", requeued)

    def _housekeeper(self) -> None:
        self.registry.flush_last_seen()
        cutoff = time.time() - self.config.telemetry_retention_hours * 3600
        removed = self.storage.purge_telemetry(cutoff)
        if removed:
            self.metrics.increment("hub_telemetry_purged_total", removed)
            log.info("purged %d telemetry rows older than %dh", removed, self.config.telemetry_retention_hours)

    def _refresh_gauges(self) -> None:
        counts = self.registry.online_counts(self.config.vehicle_offline_after_seconds)
        self.metrics.set_gauge("hub_vehicles_total", self.registry.count())
        self.metrics.set_gauge("hub_vehicles_online", counts["online"])
        self.metrics.set_gauge("hub_vehicles_offline", counts["offline"])
        self.metrics.set_gauge("hub_vehicles_inactive", counts["inactive"])
        self.metrics.set_gauge("hub_vehicles_long_polling", self.commands.waiting_vehicles)
        self.metrics.set_gauge("hub_database_bytes", self.storage.database_size_bytes())
        self.metrics.set_gauge("hub_replay_nonces_cached", self.auth.nonce_cache_size)
        command_counts = self.commands.counts_by_state()
        for state, value in command_counts.items():
            self.metrics.set_gauge(f"hub_commands_{state}", value)

    # -- readiness ----------------------------------------------------------
    def readiness(self) -> tuple[bool, dict]:
        """Is this hub fit to serve the fleet right now?"""
        checks: dict[str, object] = {}
        ok = True
        try:
            self.storage.read().execute("SELECT 1").fetchone()
            checks["database"] = "ok"
        except Exception as exc:                        # noqa: BLE001 - surfaced to the probe
            checks["database"] = f"error: {exc}"
            ok = False

        enrolled = self.registry.count()
        checks["vehicles_enrolled"] = enrolled
        checks["fleet_size_expected"] = self.config.fleet_size
        if enrolled < self.config.fleet_size:
            # Not fatal -- the hub still serves whoever is provisioned -- but an
            # operator should know the roster is short before the shift starts.
            checks["roster"] = f"warning: {self.config.fleet_size - enrolled} vehicle(s) not yet provisioned"
        else:
            checks["roster"] = "ok"

        if not self.config.operator_key:
            checks["operator_api"] = "disabled (HUB_OPERATOR_KEY unset)"
        return ok, checks
