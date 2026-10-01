"""Metrics — thread-safe counters + Prometheus text rendering."""
import threading


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {}

    def inc(self, name: str, value: float = 1.0):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def get(self, name: str) -> float:
        with self._lock:
            return self._counters.get(name, 0)

    def snapshot(self):
        with self._lock:
            return dict(self._counters)

    def render(self) -> str:
        lines = []
        for name, val in sorted(self.snapshot().items()):
            safe = name.replace(".", "_").replace("-", "_").replace("/", "_")
            lines.append(f"neuralosd_{safe} {val}")
        return "\n".join(lines) + "\n"
