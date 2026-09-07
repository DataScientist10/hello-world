"""In-process metrics, exported in Prometheus text format.

Nothing here phones home. The /metrics endpoint is scraped by whatever runs on
the depot LAN -- or simply read with curl when someone is debugging at 2am.
"""

from __future__ import annotations

import threading
import time

#: Latency buckets in seconds, sized for a LAN where anything over a second is
#: already pathological.
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._gauges: dict[str, float] = {}
        self._hist_buckets: dict[str, list[int]] = {}
        self._hist_sum: dict[str, float] = {}
        self._hist_count: dict[str, int] = {}
        self.started_at = time.time()

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            buckets = self._hist_buckets.setdefault(name, [0] * (len(BUCKETS) + 1))
            for index, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    buckets[index] += 1
            buckets[-1] += 1                        # +Inf
            self._hist_sum[name] = self._hist_sum.get(name, 0.0) + seconds
            self._hist_count[name] = self._hist_count.get(name, 0) + 1

    def render(self) -> str:
        """Prometheus exposition format."""
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hist_buckets = {k: list(v) for k, v in self._hist_buckets.items()}
            hist_sum = dict(self._hist_sum)
            hist_count = dict(self._hist_count)

        lines: list[str] = []
        by_name: dict[str, list[tuple[tuple, float]]] = {}
        for (name, labels), value in counters.items():
            by_name.setdefault(name, []).append((labels, value))
        for name, series in sorted(by_name.items()):
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(series):
                rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                suffix = "{" + rendered + "}" if rendered else ""
                lines.append(f"{name}{suffix} {value:g}")

        for name, value in sorted(gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value:g}")

        for name, buckets in sorted(hist_buckets.items()):
            lines.append(f"# TYPE {name} histogram")
            for index, bound in enumerate(BUCKETS):
                lines.append(f'{name}_bucket{{le="{bound}"}} {buckets[index]}')
            lines.append(f'{name}_bucket{{le="+Inf"}} {buckets[-1]}')
            lines.append(f"{name}_sum {hist_sum.get(name, 0.0):g}")
            lines.append(f"{name}_count {hist_count.get(name, 0)}")

        lines.append("# TYPE hub_uptime_seconds gauge")
        lines.append(f"hub_uptime_seconds {time.time() - self.started_at:g}")
        return "\n".join(lines) + "\n"

    def snapshot_counters(self) -> dict[str, float]:
        """Flat name{labels} -> value view, used by tests and /v1/status."""
        with self._lock:
            out = {}
            for (name, labels), value in self._counters.items():
                rendered = ",".join(f"{k}={v}" for k, v in labels)
                out[f"{name}{{{rendered}}}" if rendered else name] = value
            return out


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
