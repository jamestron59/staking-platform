"""The feature library.

Deliberately small. The original design called for 100+ variables — EMA 20/50/200,
RSI, MACD, VWAP, pivots, and so on. Those are not 100 pieces of information;
they are perhaps a dozen, restated. EMA20, EMA50 and EMA200 on the same series
carry correlations above 0.95, and MACD is a difference of two of them. Feeding
a model 100 collinear inputs on a few thousand samples produces a model that
fits the noise between them.

What is kept here is chosen to be as close to orthogonal as practical, and to
include the perp-specific state that generic TA has no concept of — funding
basis and open-interest dynamics, which in the original diagram were drawn as
data sources and then never used.

Sections:
  1. Microstructure  — what the book looks like right now
  2. Order flow      — who is being aggressive
  3. Price           — returns and realised volatility, strictly causal
  4. Perp            — funding, basis, open interest
"""

from __future__ import annotations

import math
from typing import Optional

from ..types import BookSnapshot, PerpContext, Side, Trade
from .base import EWMA, EWVar, Feature, LaggedValue, TimeWindow

# ---------------------------------------------------------------------------
# 1. Microstructure
# ---------------------------------------------------------------------------


class SpreadBps(Feature):
    name = "spread_bps"

    def __init__(self) -> None:
        self._v: Optional[float] = None

    def on_book(self, book: BookSnapshot) -> None:
        self._v = book.spread_bps

    def value(self) -> Optional[float]:
        return self._v


class BookImbalance(Feature):
    """(bid - ask) / (bid + ask) size within a band of the mid.

    Measured over a price band rather than the top level: top-of-book size is
    dominated by market makers flickering quotes, and reacts to noise rather
    than to pressure.
    """

    name = "book_imbalance"

    def __init__(self, band_bps: float = 5.0) -> None:
        self.band_bps = band_bps
        self._v: Optional[float] = None

    def on_book(self, book: BookSnapshot) -> None:
        b = book.depth(Side.BUY, self.band_bps)
        a = book.depth(Side.SELL, self.band_bps)
        self._v = (b - a) / (b + a) if (b + a) > 0 else None

    def value(self) -> Optional[float]:
        return self._v


class BookSlope(Feature):
    """How fast liquidity thins out as you move away from the mid.

    A steep book absorbs size cheaply; a flat one means the next market order
    travels. This is the honest, observable part of what the original design
    called 'liquidity analysis' — unlike spoofing or iceberg detection, which
    require order-level data that HL's aggregated L2 feed does not provide.
    """

    name = "book_slope"

    def __init__(self) -> None:
        self._v: Optional[float] = None

    def on_book(self, book: BookSnapshot) -> None:
        near = book.depth(Side.BUY, 5.0) + book.depth(Side.SELL, 5.0)
        far = book.depth(Side.BUY, 25.0) + book.depth(Side.SELL, 25.0)
        # `far` is a superset of `near`, so far >= near always holds. Equality
        # means the whole visible book sits inside the inner band — a compact
        # book, which is a legitimate reading of zero slope, not missing data.
        # Requiring far > near strictly would make this feature permanently
        # None on tight books and silently block the entire pipeline.
        self._v = math.log(far / near) if near > 0 else None

    def value(self) -> Optional[float]:
        return self._v


class MicropriceDeviation(Feature):
    """Microprice minus mid, in bps. A weak but genuine short-horizon predictor
    of the next mid move, because it reflects which side is thin."""

    name = "microprice_dev_bps"

    def __init__(self) -> None:
        self._v: Optional[float] = None

    def on_book(self, book: BookSnapshot) -> None:
        mid, micro = book.mid, book.microprice()
        self._v = 1e4 * (micro - mid) / mid if (mid and micro and mid > 0) else None

    def value(self) -> Optional[float]:
        return self._v


class DepthUsd(Feature):
    name = "depth_usd_10bps"

    def __init__(self) -> None:
        self._v: Optional[float] = None

    def on_book(self, book: BookSnapshot) -> None:
        mid = book.mid
        if mid:
            self._v = min(book.depth(Side.BUY, 10.0), book.depth(Side.SELL, 10.0)) * mid

    def value(self) -> Optional[float]:
        return self._v


# ---------------------------------------------------------------------------
# 2. Order flow
# ---------------------------------------------------------------------------


class CumulativeVolumeDelta(Feature):
    """Signed taker volume over a rolling window.

    Windowed rather than cumulative-since-start: an unbounded CVD is
    non-stationary and its level means nothing, only its recent change does.
    """

    name = "cvd"

    def __init__(self, window_ms: int = 300_000) -> None:
        self.window_ms = window_ms
        self._w = TimeWindow(window_ms)
        self.warmup_ms = window_ms

    def on_trade(self, trade: Trade) -> None:
        self._w.add(trade.exchange_ms, trade.signed_sz)

    def value(self) -> Optional[float]:
        return self._w.sum() if self._w.count() >= 5 else None

    def reset(self) -> None:
        self._w.clear()


class FlowImbalance(Feature):
    """CVD normalised by total volume: in [-1, 1] and comparable across regimes.

    Raw CVD scales with activity, so a threshold tuned in a quiet week fires
    constantly in a busy one.
    """

    name = "flow_imbalance"

    def __init__(self, window_ms: int = 300_000) -> None:
        self._signed = TimeWindow(window_ms)
        self._total = TimeWindow(window_ms)
        self.warmup_ms = window_ms

    def on_trade(self, trade: Trade) -> None:
        self._signed.add(trade.exchange_ms, trade.signed_sz)
        self._total.add(trade.exchange_ms, trade.sz)

    def value(self) -> Optional[float]:
        total = self._total.sum()
        return self._signed.sum() / total if total > 0 and self._total.count() >= 5 else None

    def reset(self) -> None:
        self._signed.clear()
        self._total.clear()


class TradeIntensity(Feature):
    """Trades per second, log-scaled. A regime detector: intensity spikes
    precede and accompany the moves worth trading, and identify the moments
    when spread and slippage assumptions stop holding."""

    name = "trade_intensity"

    def __init__(self, window_ms: int = 60_000) -> None:
        self._w = TimeWindow(window_ms)
        self.window_ms = window_ms
        self.warmup_ms = window_ms

    def on_trade(self, trade: Trade) -> None:
        self._w.add(trade.exchange_ms, 1.0)

    def value(self) -> Optional[float]:
        n = self._w.count()
        if n < 3:
            return None
        return math.log1p(n / (self.window_ms / 1000))

    def reset(self) -> None:
        self._w.clear()


class Absorption(Feature):
    """Volume traded per unit of price movement.

    High absorption means size is being transacted without the price going
    anywhere — someone is filling passively. This is a real, observable
    phenomenon at L2 resolution, in contrast to the iceberg detection the
    original design assumed, which needs per-order data HL does not publish.
    """

    name = "absorption"

    def __init__(self, window_ms: int = 120_000) -> None:
        self._vol = TimeWindow(window_ms)
        self._px_lag = LaggedValue(window_ms)
        self._last_px: Optional[float] = None
        self.warmup_ms = window_ms

    def on_trade(self, trade: Trade) -> None:
        self._vol.add(trade.exchange_ms, trade.sz)
        self._px_lag.add(trade.exchange_ms, trade.px)
        self._last_px = trade.px

    def value(self) -> Optional[float]:
        if self._last_px is None or self._vol.count() < 10:
            return None
        now_ms = self._px_lag.latest_ms
        past = self._px_lag.at_lag(now_ms) if now_ms is not None else None
        if past is None or past <= 0:
            return None
        move_bps = abs(1e4 * (self._last_px - past) / past)
        return math.log1p(self._vol.sum() / max(move_bps, 0.5))

    def reset(self) -> None:
        self._vol.clear()
        self._last_px = None


# ---------------------------------------------------------------------------
# 3. Price
# ---------------------------------------------------------------------------


class Return(Feature):
    """Log return over a fixed horizon, computed from lagged observations only."""

    def __init__(self, horizon_ms: int) -> None:
        self.horizon_ms = horizon_ms
        self.name = f"ret_{horizon_ms // 1000}s"
        self.warmup_ms = horizon_ms
        self._lag = LaggedValue(horizon_ms)
        self._now: Optional[float] = None
        self._now_ms = 0

    def on_book(self, book: BookSnapshot) -> None:
        mid = book.mid
        if mid and mid > 0:
            self._lag.add(book.exchange_ms, mid)
            self._now, self._now_ms = mid, book.exchange_ms

    def value(self) -> Optional[float]:
        if self._now is None:
            return None
        past = self._lag.at_lag(self._now_ms)
        if past is None or past <= 0:
            return None
        return math.log(self._now / past)

    def reset(self) -> None:
        self.__init__(self.horizon_ms)


class RealisedVol(Feature):
    """EW standard deviation of short-horizon returns, annualised-ish.

    Used for volatility-targeted sizing and for scaling stop distances, so that
    a 1% stop in a calm regime and in a violent one are not treated as the same
    risk.
    """

    name = "realised_vol"

    def __init__(self, halflife_ms: int = 300_000, sample_ms: int = 5_000) -> None:
        self.halflife_ms = halflife_ms
        self.sample_ms = sample_ms
        self.warmup_ms = halflife_ms
        self._var = EWVar(halflife_ms)
        self._last_px: Optional[float] = None
        self._last_ms = 0

    def on_book(self, book: BookSnapshot) -> None:
        mid = book.mid
        if not mid or mid <= 0:
            return
        if self._last_px is None:
            self._last_px, self._last_ms = mid, book.exchange_ms
            return
        if book.exchange_ms - self._last_ms < self.sample_ms:
            return
        self._var.update(book.exchange_ms, math.log(mid / self._last_px))
        self._last_px, self._last_ms = mid, book.exchange_ms

    def value(self) -> Optional[float]:
        return self._var.std()

    def reset(self) -> None:
        self.__init__(self.halflife_ms, self.sample_ms)


class TrendStrength(Feature):
    """Fast EWMA minus slow EWMA, normalised by realised volatility.

    One trend feature instead of three EMAs and a MACD. Normalising by
    volatility is what makes the number comparable across coins and regimes —
    an unnormalised EMA spread is a different quantity for BTC than for a
    low-priced alt, which is why thresholds tuned on one never transfer.
    """

    name = "trend_strength"

    def __init__(self, fast_ms: int = 60_000, slow_ms: int = 600_000) -> None:
        self.fast_ms, self.slow_ms = fast_ms, slow_ms
        self.warmup_ms = slow_ms
        self._fast = EWMA(fast_ms)
        self._slow = EWMA(slow_ms)
        self._vol = RealisedVol()

    def on_book(self, book: BookSnapshot) -> None:
        mid = book.mid
        if not mid or mid <= 0:
            return
        self._fast.update(book.exchange_ms, mid)
        self._slow.update(book.exchange_ms, mid)
        self._vol.on_book(book)

    def value(self) -> Optional[float]:
        f, s, v = self._fast.value, self._slow.value, self._vol.value()
        if f is None or s is None or not v or s <= 0:
            return None
        return (math.log(f / s)) / v

    def reset(self) -> None:
        self.__init__(self.fast_ms, self.slow_ms)


# ---------------------------------------------------------------------------
# 4. Perp-specific
# ---------------------------------------------------------------------------


class FundingRate(Feature):
    """Hourly funding as a rate. Both a cost and a positioning signal:
    persistently positive funding means longs are paying, which is crowding."""

    name = "funding_hourly"

    def __init__(self) -> None:
        self._v: Optional[float] = None

    def on_ctx(self, ctx: PerpContext) -> None:
        self._v = ctx.funding

    def value(self) -> Optional[float]:
        return self._v


class BasisBps(Feature):
    """Mark minus oracle, in bps. Measures how far the perp has run from spot —
    a mean-reversion pressure and an early sign of a squeeze."""

    name = "basis_bps"

    def __init__(self) -> None:
        self._v: Optional[float] = None

    def on_ctx(self, ctx: PerpContext) -> None:
        if ctx.oracle_px > 0:
            self._v = 1e4 * (ctx.mark_px - ctx.oracle_px) / ctx.oracle_px

    def value(self) -> Optional[float]:
        return self._v


class OpenInterestChange(Feature):
    """Relative change in open interest over a window.

    Combined with price direction this is one of the few genuinely
    perp-specific reads available: OI rising into a rally means new longs
    (continuation is plausible); OI falling into a rally means shorts covering
    (the move is consuming its own fuel). The original architecture drew OI as
    a data source and then never used it — this is the gap.
    """

    name = "oi_change"

    def __init__(self, window_ms: int = 900_000) -> None:
        self.window_ms = window_ms
        self.warmup_ms = window_ms
        self._lag = LaggedValue(window_ms)
        self._now: Optional[float] = None
        self._now_ms = 0

    def on_ctx(self, ctx: PerpContext) -> None:
        self._lag.add(ctx.local_ms, ctx.open_interest)
        self._now, self._now_ms = ctx.open_interest, ctx.local_ms

    def value(self) -> Optional[float]:
        if self._now is None:
            return None
        past = self._lag.at_lag(self._now_ms)
        return (self._now - past) / past if past and past > 0 else None

    def reset(self) -> None:
        self.__init__(self.window_ms)


def default_feature_set() -> list[Feature]:
    """Fourteen features, chosen for low mutual correlation.

    Fewer than the original 100+, and the reduction is the point: with a few
    thousand training samples, every additional collinear input buys variance
    without buying information.
    """
    return [
        SpreadBps(),
        BookImbalance(),
        BookSlope(),
        MicropriceDeviation(),
        DepthUsd(),
        CumulativeVolumeDelta(),
        FlowImbalance(),
        TradeIntensity(),
        Absorption(),
        Return(60_000),
        Return(300_000),
        RealisedVol(),
        TrendStrength(),
        FundingRate(),
        BasisBps(),
        OpenInterestChange(),
    ]
