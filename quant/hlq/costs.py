"""The cost model.

This module is the reason most retail strategies that "work" do not work. On a
5-minute horizon a directional edge of 3-4 bps is a good edge; a taker round
trip on HL costs roughly 9 bps in fees alone before any slippage. The
arithmetic is not subtle, it is just usually left out.

So cost is not a post-hoc adjustment applied to a backtest curve. It is an
input to the decision: `RiskEngine` refuses any intent whose expected edge does
not exceed `estimate_round_trip` by the configured margin.

Four components, kept separate because they behave differently:
  - fees: deterministic, known in advance, differ maker vs taker;
  - spread/impact: depends on size vs the book right now;
  - funding: depends on how long you hold and which way you lean;
  - residual: queue position, latency, adverse selection — the part you can
    only learn from your own fills, which `SlippageCalibrator` measures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .data.book import slippage_bps, walk_book
from .types import BookSnapshot, Fill, Side


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Every number in bps of notional, positive = cost."""

    entry_fee_bps: float
    exit_fee_bps: float
    entry_impact_bps: float
    exit_impact_bps: float
    funding_bps: float
    residual_bps: float

    @property
    def total_bps(self) -> float:
        return (
            self.entry_fee_bps
            + self.exit_fee_bps
            + self.entry_impact_bps
            + self.exit_impact_bps
            + self.funding_bps
            + self.residual_bps
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "entry_fee_bps": round(self.entry_fee_bps, 3),
            "exit_fee_bps": round(self.exit_fee_bps, 3),
            "entry_impact_bps": round(self.entry_impact_bps, 3),
            "exit_impact_bps": round(self.exit_impact_bps, 3),
            "funding_bps": round(self.funding_bps, 3),
            "residual_bps": round(self.residual_bps, 3),
            "total_bps": round(self.total_bps, 3),
        }


class CostModel:
    def __init__(
        self,
        *,
        taker_fee_bps: float,
        maker_fee_bps: float,
        residual_slippage_bps: float = 1.0,
        funding_interval_hours: float = 1.0,
    ) -> None:
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.residual_slippage_bps = residual_slippage_bps
        self.funding_interval_hours = funding_interval_hours

    def fee_bps(self, is_maker: bool) -> float:
        return self.maker_fee_bps if is_maker else self.taker_fee_bps

    def impact_bps(self, book: BookSnapshot, side: Side, size: float, is_maker: bool) -> Optional[float]:
        """Maker orders pay no spread by construction — but only if they fill.

        Charging a maker order zero cost is the mirror-image error of charging
        a taker order zero slippage: it ignores that unfilled maker orders are
        exactly the ones where the market moved against you. That adverse
        selection is not priced here; it is priced by the matching engine in
        `sim.matching`, which simply does not fill orders the market ran away
        from, and by `residual_bps` once calibrated.
        """
        if is_maker:
            return 0.0
        return slippage_bps(book, side, size)

    def funding_bps(self, funding_rate_hourly: float, hold_hours: float, side: Side) -> float:
        """Longs pay a positive funding rate, shorts receive it.

        Returned as a cost, so a short in a positive-funding regime yields a
        negative number — a genuine carry credit, and on HL frequently larger
        than the directional edge being chased.
        """
        return 1e4 * funding_rate_hourly * hold_hours * side.sign

    def estimate_round_trip(
        self,
        book: BookSnapshot,
        side: Side,
        size: float,
        *,
        maker_entry: bool = False,
        maker_exit: bool = False,
        hold_hours: float = 0.0,
        funding_rate_hourly: float = 0.0,
    ) -> Optional[CostBreakdown]:
        """None means the book cannot absorb this size: a veto, not a cost."""
        entry_impact = self.impact_bps(book, side, size, maker_entry)
        exit_impact = self.impact_bps(book, side.opposite, size, maker_exit)
        if entry_impact is None or exit_impact is None:
            return None
        return CostBreakdown(
            entry_fee_bps=self.fee_bps(maker_entry),
            exit_fee_bps=self.fee_bps(maker_exit),
            entry_impact_bps=entry_impact,
            exit_impact_bps=exit_impact,
            funding_bps=self.funding_bps(funding_rate_hourly, hold_hours, side),
            # Charged once per leg.
            residual_bps=2 * self.residual_slippage_bps,
        )

    def breakeven_move_bps(self, cost: CostBreakdown) -> float:
        """How far price must travel just to get back to flat. The number to
        compare a model's predicted move against — not the win rate."""
        return cost.total_bps

    def max_size_for_impact(
        self, book: BookSnapshot, side: Side, max_impact_bps: float
    ) -> float:
        """Largest size whose sweep cost stays under a bound. Used by the
        router to slice rather than to guess a participation rate."""
        mid = book.mid
        if mid is None:
            return 0.0
        levels = book.asks if side is Side.BUY else book.bids
        limit_px = mid * (1 + side.sign * max_impact_bps / 1e4)
        total = 0.0
        for lv in levels:
            if (side is Side.BUY and lv.px > limit_px) or (side is Side.SELL and lv.px < limit_px):
                break
            total += lv.sz
        return total


class SlippageCalibrator:
    """Learns `residual_slippage_bps` from realised fills.

    Every order records the decision price (mid at decision time) and the
    intended price. When the fill arrives we compare. The gap is the part of
    execution cost that no book-walk model predicted: queue position, latency,
    and being picked off.

    Feed this back into config rather than trusting the default — an assumed
    residual is an assumption about your own infrastructure, and you cannot
    know it before you have run.
    """

    def __init__(self, window: int = 500) -> None:
        self.window = window
        self._samples: list[float] = []

    def observe(self, decision_mid: float, fill: Fill) -> Optional[float]:
        if decision_mid <= 0:
            return None
        # Positive = we did worse than the mid we decided at.
        slip = 1e4 * (fill.px - decision_mid) / decision_mid * fill.side.sign
        self._samples.append(slip)
        if len(self._samples) > self.window:
            self._samples.pop(0)
        return slip

    @property
    def count(self) -> int:
        return len(self._samples)

    def summary(self) -> dict[str, float]:
        if not self._samples:
            return {}
        s = sorted(self._samples)
        n = len(s)

        def pct(p: float) -> float:
            return s[min(n - 1, int(p * n))]

        return {
            "n": n,
            "mean_bps": round(sum(s) / n, 3),
            "median_bps": round(pct(0.5), 3),
            "p90_bps": round(pct(0.9), 3),
            "p99_bps": round(pct(0.99), 3),
            "worst_bps": round(s[-1], 3),
        }

    def suggested_residual_bps(self) -> Optional[float]:
        """Use the mean, not the median: the tail is a real cost you pay."""
        if len(self._samples) < 50:
            return None
        return round(sum(self._samples) / len(self._samples), 3)
