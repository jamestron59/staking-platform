"""Strategy interface.

A strategy proposes; it never sizes, never sends, and never overrides risk. It
receives a point-in-time feature vector and the current book, and returns an
intent or None.

Note what an intent must carry: `expected_edge_bps` and `stop_px`. Both are
mandatory in practice because the risk engine cannot function without them —
edge is what gets compared to cost, and the stop is what determines size. A
strategy that cannot state how far it expects price to move has not finished
being a strategy, it is a direction guess.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..features.pipeline import FeatureVector
from ..types import BookSnapshot, OrderIntent, PerpContext, Position


class Strategy(ABC):
    name: str = "unnamed"

    @abstractmethod
    def on_features(
        self,
        *,
        features: FeatureVector,
        book: BookSnapshot,
        position: Optional[Position],
        ctx: Optional[PerpContext],
        now_ms: int,
    ) -> Optional[OrderIntent]: ...

    def on_fill(self, *args, **kwargs) -> None:
        pass

    def min_interval_ms(self) -> int:
        """Minimum gap between evaluations. Prevents a strategy from firing on
        every book update, which produces hundreds of near-identical intents a
        minute and turns the cost model into the only thing that matters."""
        return 5_000


class Throttle:
    """Per-coin rate limiter for strategy evaluation."""

    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self._last: dict[str, int] = {}

    def ready(self, coin: str, now_ms: int) -> bool:
        last = self._last.get(coin, 0)
        if now_ms - last < self.interval_ms:
            return False
        self._last[coin] = now_ms
        return True
