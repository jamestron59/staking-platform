"""Feature primitives, built so that lookahead cannot happen.

The usual approach — load history into a dataframe, compute indicators with
rolling windows, then align to labels — makes lookahead a discipline problem.
One centred window, one `shift()` with the wrong sign, one label built before
the feature, and the model trains on the future. The bug is invisible: the
backtest simply looks excellent.

Here features are **incremental state machines**. A feature can only ever have
observed events that were fed to it, one at a time, in arrival order. There is
no array to index into and therefore no index to get wrong. `value()` returns
what is knowable *now*, or None if not enough history has arrived.

The cost is that features must be expressed as online updates rather than
vectorised expressions, which is more work to write. That is the correct trade:
the vectorised version is faster to write once and wrong in ways you cannot see.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque, Optional

from ..types import BookSnapshot, PerpContext, Trade


class Feature(ABC):
    """One scalar, updated online.

    `warmup_ms` declares how much history the feature needs before its value is
    meaningful. The pipeline refuses to emit a vector until every feature in it
    is ready, which prevents a model from training on the first thirty seconds
    after a restart when half the inputs are still zero.
    """

    name: str = "unnamed"
    warmup_ms: int = 0

    def on_book(self, book: BookSnapshot) -> None:  # noqa: B027 - optional hook
        pass

    def on_trade(self, trade: Trade) -> None:  # noqa: B027
        pass

    def on_ctx(self, ctx: PerpContext) -> None:  # noqa: B027
        pass

    @abstractmethod
    def value(self) -> Optional[float]: ...

    @property
    def ready(self) -> bool:
        return self.value() is not None

    def reset(self) -> None:
        """Called after a feed gap. Derived state computed across a gap is
        wrong in ways that do not announce themselves — a CVD that silently
        skipped four minutes of trades still returns a plausible number."""
        self.__init__()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Online accumulators
# ---------------------------------------------------------------------------


class TimeWindow:
    """Time-based rolling window. Bounded by elapsed time, not sample count.

    Count-based windows are wrong for irregular event streams: 'the last 100
    trades' covers ten seconds in a burst and ten minutes when quiet, so the
    feature silently changes meaning with market activity.
    """

    __slots__ = ("window_ms", "_items", "_sum")

    def __init__(self, window_ms: int) -> None:
        self.window_ms = window_ms
        self._items: Deque[tuple[int, float]] = deque()
        self._sum = 0.0

    def add(self, ts_ms: int, value: float) -> None:
        self._items.append((ts_ms, value))
        self._sum += value
        self._evict(ts_ms)

    def _evict(self, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        while self._items and self._items[0][0] < cutoff:
            self._sum -= self._items.popleft()[1]

    def sum(self, now_ms: Optional[int] = None) -> float:
        if now_ms is not None:
            self._evict(now_ms)
        return self._sum

    def mean(self, now_ms: Optional[int] = None) -> Optional[float]:
        if now_ms is not None:
            self._evict(now_ms)
        return self._sum / len(self._items) if self._items else None

    def count(self) -> int:
        return len(self._items)

    def span_ms(self) -> int:
        return (self._items[-1][0] - self._items[0][0]) if len(self._items) > 1 else 0

    def values(self) -> list[float]:
        return [v for _, v in self._items]

    def clear(self) -> None:
        self._items.clear()
        self._sum = 0.0


class EWMA:
    """Exponentially weighted mean with a time-based half-life.

    Time-based rather than sample-based for the same reason as TimeWindow: the
    decay must mean the same thing at 3am as during a liquidation cascade.
    """

    __slots__ = ("halflife_ms", "_value", "_last_ms")

    def __init__(self, halflife_ms: int) -> None:
        self.halflife_ms = max(1, halflife_ms)
        self._value: Optional[float] = None
        self._last_ms = 0

    def update(self, ts_ms: int, x: float) -> float:
        if self._value is None:
            self._value, self._last_ms = x, ts_ms
            return x
        dt = max(0, ts_ms - self._last_ms)
        alpha = 1.0 - math.pow(0.5, dt / self.halflife_ms)
        self._value += alpha * (x - self._value)
        self._last_ms = ts_ms
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value


class EWVar:
    """Exponentially weighted variance, for realised volatility."""

    __slots__ = ("_mean", "_var", "halflife_ms", "_last_ms", "_n")

    def __init__(self, halflife_ms: int) -> None:
        self.halflife_ms = max(1, halflife_ms)
        self._mean: Optional[float] = None
        self._var = 0.0
        self._last_ms = 0
        self._n = 0

    def update(self, ts_ms: int, x: float) -> None:
        self._n += 1
        if self._mean is None:
            self._mean, self._last_ms = x, ts_ms
            return
        dt = max(0, ts_ms - self._last_ms)
        alpha = 1.0 - math.pow(0.5, dt / self.halflife_ms)
        delta = x - self._mean
        self._mean += alpha * delta
        self._var = (1 - alpha) * (self._var + alpha * delta * delta)
        self._last_ms = ts_ms

    def std(self) -> Optional[float]:
        if self._n < 10 or self._var <= 0:
            return None
        return math.sqrt(self._var)


class LaggedValue:
    """Value as of `lag_ms` ago. The building block for any return feature.

    Keeps a timestamped history and returns the most recent observation at or
    before `now - lag`. Notably it does NOT interpolate: an interpolated value
    at time t is computed from observations after t, which is lookahead wearing
    a disguise.
    """

    __slots__ = ("lag_ms", "_hist")

    def __init__(self, lag_ms: int) -> None:
        self.lag_ms = lag_ms
        self._hist: Deque[tuple[int, float]] = deque()

    def add(self, ts_ms: int, value: float) -> None:
        self._hist.append((ts_ms, value))
        cutoff = ts_ms - self.lag_ms * 3
        while len(self._hist) > 2 and self._hist[0][0] < cutoff:
            self._hist.popleft()

    def at_lag(self, now_ms: int) -> Optional[float]:
        target = now_ms - self.lag_ms
        best: Optional[float] = None
        for ts, v in self._hist:
            if ts <= target:
                best = v
            else:
                break
        return best

    def span_ms(self) -> int:
        return (self._hist[-1][0] - self._hist[0][0]) if len(self._hist) > 1 else 0

    @property
    def latest_ms(self) -> Optional[int]:
        return self._hist[-1][0] if self._hist else None

    @property
    def latest(self) -> Optional[float]:
        return self._hist[-1][1] if self._hist else None
