"""Disk-pressure handling.

Retention alone is not a defence. It purges on a timer against a fixed time
cutoff, which assumes the sample rate, the fleet size and the disk are all what
they were on the day the hub was configured. Add fifty vehicles, raise the
sample rate, or hand the depot a smaller disk, and the disk fills between two
purges. SQLite then starts failing writes, and a hub that cannot write is a hub
that cannot dispatch commands.

So the disk is watched directly, and pressure produces a deliberate response
rather than a crash. The response is asymmetric, and that asymmetry is the
whole design:

* **Telemetry is shed.** It is deferrable by construction -- every vehicle
  already buffers to its own spool and retries, so refusing an upload costs
  latency on the fleet history, not data.
* **Commands are protected.** A ``pull_over`` is the reason this system exists.
  Command dispatch keeps working while the hub is shedding telemetry, because
  the alternative is a depot that cannot stop its vehicles.

Recovery uses hysteresis: the hub stops shedding only once free space is back
above the *warning* line, not merely back over the critical one, so it does not
flap between states while sitting on the threshold.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: Disk states, in increasing order of severity.
OK = "ok"
WARNING = "warning"
CRITICAL = "critical"

#: Numeric form for the metrics gauge, which cannot carry a string.
STATE_CODES = {OK: 0, WARNING: 1, CRITICAL: 2}


@dataclass
class DiskState:
    state: str
    free_bytes: int
    total_bytes: int
    free_ratio: float
    shedding: bool

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "free_ratio": round(self.free_ratio, 4),
            "shedding_telemetry": self.shedding,
        }


def _default_usage(path: Path) -> tuple[int, int]:
    usage = shutil.disk_usage(path)
    return usage.free, usage.total


class DiskGuard:
    """Tracks free space on the data volume and decides what to shed.

    ``usage`` is injectable so the thresholds can be tested without filling a
    real disk.
    """

    def __init__(self, path: Path | str, config, usage: Callable[[Path], tuple[int, int]] | None = None) -> None:
        self._path = Path(path)
        self._config = config
        self._usage = usage or _default_usage
        self._lock = threading.Lock()
        self._shedding = False
        self._state = DiskState(OK, 0, 0, 1.0, False)

    # -- measurement --------------------------------------------------------
    def sample(self) -> DiskState:
        """Re-read the disk and update the shedding decision."""
        try:
            free, total = self._usage(self._path)
        except OSError:
            # If the volume cannot even be stat-ed, assume the worst: shedding
            # telemetry is recoverable, silently filling a disk is not.
            with self._lock:
                self._shedding = True
                self._state = DiskState(CRITICAL, 0, 0, 0.0, True)
                return self._state

        ratio = (free / total) if total else 1.0
        critical = free < self._config.disk_critical_bytes(total)
        warning = free < self._config.disk_warn_bytes(total)

        with self._lock:
            if critical:
                state = CRITICAL
                self._shedding = True
            elif warning:
                state = WARNING
                # Hysteresis: still below the warning line, so stay shedding if
                # we already were. Flapping in and out would give the fleet a
                # stream of alternating 200s and 503s.
                pass
            else:
                state = OK
                self._shedding = False

            self._state = DiskState(state, free, total, ratio, self._shedding)
            return self._state

    # -- decisions ----------------------------------------------------------
    @property
    def shedding_telemetry(self) -> bool:
        """True when the hub should refuse telemetry uploads.

        Commands are deliberately never shed: this property is consulted only
        on the ingest path.
        """
        with self._lock:
            return self._shedding

    @property
    def state(self) -> DiskState:
        with self._lock:
            return self._state

    @property
    def under_pressure(self) -> bool:
        with self._lock:
            return self._state.state in (WARNING, CRITICAL)

    def bytes_to_reclaim(self) -> int:
        """How much space a purge should try to free to clear the warning line."""
        with self._lock:
            state = self._state
        if not state.total_bytes:
            return 0
        target = self._config.disk_warn_bytes(state.total_bytes)
        return max(0, int(target - state.free_bytes))
