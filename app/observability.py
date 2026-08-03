"""Dependency-free counters for operators and scrape-based monitoring."""

from collections import Counter
from threading import Lock


class Metrics:
    def __init__(self) -> None:
        self._values: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        if not name.replace("_", "").isalnum():
            raise ValueError("metric names must be alphanumeric with underscores")
        with self._lock:
            self._values[name] += amount

    def render(self) -> str:
        with self._lock:
            return "".join(
                f"forecastfoundry_{name} {value}\n" for name, value in self._values.items()
            )
