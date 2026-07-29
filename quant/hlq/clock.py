"""Time. One abstraction so that backtest and live share every code path.

Rule enforced everywhere downstream: strategy and feature code may only ever
call `clock.now_ms()`. It must never call `time.time()` directly, because in a
backtest that would silently read wall-clock time and produce a system that
works in research and breaks in production (or worse, the reverse).
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class LiveClock:
    __slots__ = ()

    def now_ms(self) -> int:
        return int(time.time() * 1000)


class SimClock:
    """Driven by the event stream in a backtest: `advance` is called by the
    engine with each event's exchange timestamp, and never moves backwards."""

    __slots__ = ("_now_ms",)

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ts_ms: int) -> None:
        if ts_ms < self._now_ms:
            raise ValueError(f"clock moved backwards: {self._now_ms} -> {ts_ms}")
        self._now_ms = ts_ms


class LatencyModel:
    """Backtests that assume zero latency systematically overstate results.

    We charge two separate delays:
      - `decision_ms`: event received -> order submitted (feature compute + inference)
      - `wire_ms`: order submitted -> visible to the matching engine

    Both default to deliberately pessimistic values. Replace them with measured
    percentiles from `ops/metrics` once live telemetry exists — the point is
    that the number is explicit rather than implicitly zero.
    """

    __slots__ = ("decision_ms", "wire_ms")

    def __init__(self, decision_ms: int = 25, wire_ms: int = 120) -> None:
        self.decision_ms = decision_ms
        self.wire_ms = wire_ms

    @property
    def total_ms(self) -> int:
        return self.decision_ms + self.wire_ms

    def arrival_ms(self, decision_ts_ms: int) -> int:
        return decision_ts_ms + self.total_ms
