"""The feature pipeline — one instance, used by backtest, paper and live alike.

This is the single most important structural decision in the system. If
research computes features with pandas and production computes them with
streaming code, the two will disagree, and the disagreement is invisible: the
model trains on one distribution and is served another. That is train/serve
skew, and it degrades live performance in a way that looks exactly like the
edge decaying.

Here there is only one implementation. The backtester feeds it recorded events;
the live loop feeds it websocket events. Same objects, same order, same code.
`hlq.model.dataset` builds training data by replaying recordings through this
pipeline, so the training vectors are literally produced by the production code.

Feed gaps are handled by resetting affected features rather than carrying state
across the discontinuity, and by refusing to emit vectors until warmup has
elapsed again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..logging_setup import get_logger
from ..types import BookSnapshot, PerpContext, Trade
from .base import Feature
from .library import default_feature_set

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FeatureVector:
    coin: str
    ts_ms: int
    values: dict[str, float]
    complete: bool

    def as_list(self, names: list[str]) -> list[float]:
        return [self.values.get(n, 0.0) for n in names]


class CoinPipeline:
    """Feature state for one coin."""

    def __init__(self, coin: str, features: list[Feature]) -> None:
        self.coin = coin
        self.features = features
        self._first_ms: Optional[int] = None
        self._last_ms = 0
        self._warmup_ms = max((f.warmup_ms for f in features), default=0)

    def on_book(self, book: BookSnapshot) -> None:
        self._mark(book.exchange_ms)
        for f in self.features:
            f.on_book(book)

    def on_trade(self, trade: Trade) -> None:
        self._mark(trade.exchange_ms)
        for f in self.features:
            f.on_trade(trade)

    def on_ctx(self, ctx: PerpContext) -> None:
        self._mark(ctx.local_ms)
        for f in self.features:
            f.on_ctx(ctx)

    def _mark(self, ts_ms: int) -> None:
        if self._first_ms is None:
            self._first_ms = ts_ms
        self._last_ms = max(self._last_ms, ts_ms)

    def on_gap(self) -> None:
        """After a reconnect: reset every feature and restart warmup.

        Carrying a CVD across a four-minute hole produces a number that looks
        entirely reasonable and is wrong. Restarting the warmup clock means the
        strategy simply does not trade until state is trustworthy again.
        """
        log.warn("feature_gap_reset", coin=self.coin, features=len(self.features))
        for f in self.features:
            f.reset()
        self._first_ms = None

    @property
    def warm(self) -> bool:
        return (
            self._first_ms is not None
            and (self._last_ms - self._first_ms) >= self._warmup_ms
        )

    def snapshot(self, ts_ms: Optional[int] = None) -> FeatureVector:
        """The feature vector as of now. Never peeks at anything not yet fed."""
        values: dict[str, float] = {}
        missing = []
        for f in self.features:
            v = f.value()
            if v is None:
                missing.append(f.name)
            else:
                values[f.name] = float(v)
        return FeatureVector(
            coin=self.coin,
            ts_ms=ts_ms if ts_ms is not None else self._last_ms,
            values=values,
            complete=self.warm and not missing,
        )

    def missing(self) -> list[str]:
        return [f.name for f in self.features if f.value() is None]


class FeaturePipeline:
    """Multi-coin dispatcher. Owns one `CoinPipeline` per symbol."""

    def __init__(self, coins: Iterable[str], factory=default_feature_set) -> None:
        self._factory = factory
        self._pipes: dict[str, CoinPipeline] = {
            c: CoinPipeline(c, factory()) for c in coins
        }
        self.feature_names: list[str] = [f.name for f in factory()]

    def pipeline_for(self, coin: str) -> Optional[CoinPipeline]:
        return self._pipes.get(coin)

    def on_book(self, book: BookSnapshot, after_gap: bool = False) -> None:
        p = self._pipes.get(book.coin)
        if p is None:
            return
        if after_gap:
            p.on_gap()
        p.on_book(book)

    def on_trade(self, trade: Trade, after_gap: bool = False) -> None:
        p = self._pipes.get(trade.coin)
        if p is None:
            return
        if after_gap:
            p.on_gap()
        p.on_trade(trade)

    def on_ctx(self, ctx: PerpContext, after_gap: bool = False) -> None:
        p = self._pipes.get(ctx.coin)
        if p is None:
            return
        if after_gap:
            p.on_gap()
        p.on_ctx(ctx)

    def dispatch(self, event) -> None:
        """Route a `data.replay.Event`. The single entry point used by both the
        backtester and the live loop, so neither can drift from the other."""
        payload = event.payload
        if isinstance(payload, BookSnapshot):
            self.on_book(payload, event.after_gap)
        elif isinstance(payload, Trade):
            self.on_trade(payload, event.after_gap)
        elif isinstance(payload, PerpContext):
            self.on_ctx(payload, event.after_gap)

    def snapshot(self, coin: str, ts_ms: Optional[int] = None) -> Optional[FeatureVector]:
        p = self._pipes.get(coin)
        return p.snapshot(ts_ms) if p else None

    def ready_coins(self) -> list[str]:
        return [c for c, p in self._pipes.items() if p.warm]

    def diagnostics(self) -> dict[str, dict]:
        return {
            c: {"warm": p.warm, "missing": p.missing()}
            for c, p in self._pipes.items()
        }
