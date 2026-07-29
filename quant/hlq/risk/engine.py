"""The risk engine: the last thing between a signal and the exchange.

It answers one question — "how much, if any?" — and it is the only component
allowed to say no. Strategies propose; this disposes.

The check that does the most work is not the position-size formula, it is
`_check_edge_vs_cost`. A 0.5%-per-trade rule bounds the loss on any single
trade but says nothing about whether the trade has positive expectancy. A
system that risks 0.5% on a thousand coin-flips with 9bps of round-trip cost
loses money with perfect risk discipline the whole way down.

Portfolio risk is aggregated with an explicit correlation assumption:

    portfolio_risk = sqrt( (1-p)*sum(r_i^2) + p*(sum(r_i))^2 )

At p=0 this is the naive independent case, at p=1 it degenerates to plain
addition. Crypto perps in a liquidation cascade behave like p≈1, so the config
default of 0.8 is not conservatism, it is realism.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..config import RiskConfig
from ..costs import CostBreakdown, CostModel
from ..instruments import Instrument, Rounding
from ..logging_setup import get_logger
from ..types import BookSnapshot, OrderIntent, Position, Side
from . import liquidation
from .killswitch import KillSwitch

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    size: float = 0.0
    notional_usd: float = 0.0
    reason: str = ""
    cost: Optional[CostBreakdown] = None
    liquidation: Optional[liquidation.LiquidationEstimate] = None
    diagnostics: dict = field(default_factory=dict)

    @staticmethod
    def veto(reason: str, **diag) -> "RiskDecision":
        return RiskDecision(approved=False, reason=reason, diagnostics=diag)


class RiskEngine:
    def __init__(
        self,
        cfg: RiskConfig,
        cost_model: CostModel,
        kill_switch: KillSwitch,
    ) -> None:
        self.cfg = cfg
        self.costs = cost_model
        self.kill = kill_switch
        self.equity_usd = cfg.equity_usd

    def update_equity(self, equity_usd: float) -> None:
        self.equity_usd = equity_usd

    # ---- main entry point -------------------------------------------------

    def evaluate(
        self,
        intent: OrderIntent,
        instrument: Instrument,
        book: BookSnapshot,
        positions: dict[str, Position],
        *,
        funding_rate_hourly: float = 0.0,
        expected_hold_hours: float = 0.25,
        maker_entry: bool = True,
        marks: Optional[dict[str, float]] = None,
    ) -> RiskDecision:
        if self.kill.tripped and not intent.reduce_only:
            return RiskDecision.veto("kill_switch_tripped", events=self.kill.status()["events"])

        mid = book.mid
        if mid is None or mid <= 0:
            return RiskDecision.veto("no_two_sided_market")

        if not intent.reduce_only:
            gate = self._check_market_quality(book, mid)
            if gate:
                return gate

        # 1. Size from the stop, not from a fixed notional. The stop distance is
        #    what determines how much a "0.5% risk" trade may actually buy.
        size = self._size_from_stop(intent, instrument, mid)
        if size <= 0:
            return RiskDecision.veto("stop_distance_yields_zero_size", stop_px=intent.stop_px, mid=mid)

        # 2. Hard caps. Applied before anything clever, so a bug in the clever
        #    part cannot exceed them.
        size, cap_reason = self._apply_caps(size, instrument, mid, positions, intent, marks or {})
        if size <= 0:
            return RiskDecision.veto(cap_reason or "capped_to_zero")

        # 3. Portfolio-level, correlation-adjusted.
        if not intent.reduce_only:
            size = self._apply_portfolio_risk(size, instrument, mid, positions, intent)
            if size <= 0:
                return RiskDecision.veto(
                    "portfolio_risk_budget_exhausted",
                    current_risk_pct=round(self._portfolio_risk_pct(positions, {}), 3),
                    limit_pct=self.cfg.max_portfolio_risk_pct,
                )

        size = instrument.round_size(size)
        if size <= 0 or not instrument.meets_min_notional(mid, size):
            return RiskDecision.veto(
                "below_min_notional",
                size=size, notional=round(size * mid, 2), min_usd=instrument.min_notional_usd,
            )

        # 4. Cost gate. Runs on the FINAL size, because impact is size-dependent
        #    and a size that passes at $100 may not at $500.
        cost = self.costs.estimate_round_trip(
            book, intent.side, size,
            maker_entry=maker_entry, maker_exit=False,
            hold_hours=expected_hold_hours, funding_rate_hourly=funding_rate_hourly,
        )
        if cost is None:
            return RiskDecision.veto("book_cannot_absorb_size", size=size)
        if not intent.reduce_only:
            edge_gate = self._check_edge_vs_cost(intent, cost)
            if edge_gate:
                return edge_gate

        # 5. Liquidation buffer.
        liq = self._assess_liquidation(intent, instrument, mid, size, positions)
        if liq is not None and not liq.survivable:
            return RiskDecision.veto("insufficient_liquidation_buffer", note=liq.note,
                                     buffer=liq.buffer_multiple)

        return RiskDecision(
            approved=True,
            size=size,
            notional_usd=round(size * mid, 2),
            reason="ok",
            cost=cost,
            liquidation=liq,
            diagnostics={
                "edge_bps": intent.expected_edge_bps,
                "cost_bps": round(cost.total_bps, 3),
                "net_edge_bps": round(intent.expected_edge_bps - cost.total_bps, 3),
                "spread_bps": round(book.spread_bps or 0, 3),
                "risk_pct_of_equity": round(self._risk_pct(size, mid, intent), 4),
            },
        )

    # ---- individual gates -------------------------------------------------

    def _check_market_quality(self, book: BookSnapshot, mid: float) -> Optional[RiskDecision]:
        spread = book.spread_bps
        if spread is None or spread > self.cfg.max_spread_bps:
            return RiskDecision.veto("spread_too_wide", spread_bps=spread, limit=self.cfg.max_spread_bps)
        depth_usd = min(
            book.depth(Side.BUY, 10.0), book.depth(Side.SELL, 10.0)
        ) * mid
        if depth_usd < self.cfg.min_depth_usd_at_10bps:
            return RiskDecision.veto(
                "insufficient_depth", depth_usd=round(depth_usd), required=self.cfg.min_depth_usd_at_10bps
            )
        return None

    def _check_edge_vs_cost(self, intent: OrderIntent, cost: CostBreakdown) -> Optional[RiskDecision]:
        net = intent.expected_edge_bps - cost.total_bps
        if net < self.cfg.min_edge_after_costs_bps:
            return RiskDecision.veto(
                "edge_does_not_cover_costs",
                edge_bps=round(intent.expected_edge_bps, 3),
                cost_bps=round(cost.total_bps, 3),
                net_bps=round(net, 3),
                required_bps=self.cfg.min_edge_after_costs_bps,
                breakdown=cost.as_dict(),
            )
        return None

    # ---- sizing -----------------------------------------------------------

    def _size_from_stop(self, intent: OrderIntent, inst: Instrument, mid: float) -> float:
        entry = intent.limit_px or mid
        if intent.reduce_only:
            return intent.target_notional_usd / entry if entry > 0 else 0.0
        if intent.stop_px is None:
            # No stop means no defined risk. Fall back to the notional cap, which
            # is a far worse bound, and say so.
            log.warn("intent_without_stop", coin=intent.coin, reason=intent.reason)
            return min(intent.target_notional_usd, self.cfg.max_position_notional_usd) / entry
        stop_dist = abs(entry - intent.stop_px)
        if stop_dist <= 0:
            return 0.0
        risk_usd = self.equity_usd * self.cfg.risk_per_trade_pct / 100.0
        return risk_usd / stop_dist

    def _apply_caps(
        self, size: float, inst: Instrument, mid: float,
        positions: dict[str, Position], intent: OrderIntent, marks: dict[str, float],
    ) -> tuple[float, str]:
        notional = size * mid

        if notional > self.cfg.max_position_notional_usd:
            size = self.cfg.max_position_notional_usd / mid
            notional = size * mid

        # Each position must be valued at ITS OWN price. Using the price of the
        # coin currently under evaluation would value 0.1 ETH at 0.1 x the BTC
        # price — off by more than an order of magnitude, in the direction that
        # silently blocks or oversizes trades depending on which coin fires.
        # Falling back to entry price is approximate but never wildly wrong.
        gross = sum(
            abs(p.size) * marks.get(c, p.entry_px)
            for c, p in positions.items()
            if c != intent.coin and not p.is_flat
        )
        room = self.cfg.max_gross_notional_usd - gross
        if room <= 0:
            return 0.0, "gross_notional_cap_reached"
        if notional > room:
            size = room / mid
            notional = size * mid

        max_by_leverage = self.equity_usd * self.cfg.max_leverage
        if gross + notional > max_by_leverage:
            size = max(0.0, (max_by_leverage - gross)) / mid

        open_count = sum(1 for p in positions.values() if not p.is_flat)
        if intent.coin not in positions or positions[intent.coin].is_flat:
            if open_count >= self.cfg.max_concurrent_positions:
                return 0.0, "max_concurrent_positions_reached"

        return size, ""

    def _risk_pct(self, size: float, mid: float, intent: OrderIntent) -> float:
        """Risk of one position as a percentage of equity, measured at its stop."""
        if intent.stop_px is None or self.equity_usd <= 0:
            return size * mid / self.equity_usd * 100 if self.equity_usd > 0 else 0.0
        entry = intent.limit_px or mid
        return abs(entry - intent.stop_px) * size / self.equity_usd * 100

    def _portfolio_risk_pct(self, positions: dict[str, Position], extra: dict[str, float]) -> float:
        """Correlation-adjusted aggregate. Without the adjustment, five 0.5%
        positions read as 'well within a 1.5% budget' while actually carrying
        close to 2.5% of joint downside."""
        risks = [
            abs(p.size) * p.entry_px * 0.01 / self.equity_usd * 100
            for p in positions.values() if not p.is_flat and self.equity_usd > 0
        ]
        risks.extend(extra.values())
        if not risks:
            return 0.0
        p = self.cfg.assumed_correlation
        ssq = sum(r * r for r in risks)
        s = sum(risks)
        return math.sqrt(max(0.0, (1 - p) * ssq + p * s * s))

    def _apply_portfolio_risk(
        self, size: float, inst: Instrument, mid: float,
        positions: dict[str, Position], intent: OrderIntent,
    ) -> float:
        budget = self.cfg.max_portfolio_risk_pct
        current = self._portfolio_risk_pct(positions, {})
        if current >= budget:
            return 0.0
        candidate = self._risk_pct(size, mid, intent)
        combined = self._portfolio_risk_pct(positions, {intent.coin: candidate})
        if combined <= budget:
            return size
        # Shrink until it fits. Bisection rather than algebra because the
        # correlation term makes the inverse awkward and this runs once per intent.
        lo, hi = 0.0, size
        for _ in range(40):
            mididx = (lo + hi) / 2
            r = self._risk_pct(mididx, mid, intent)
            if self._portfolio_risk_pct(positions, {intent.coin: r}) <= budget:
                lo = mididx
            else:
                hi = mididx
        return lo

    def _assess_liquidation(
        self, intent: OrderIntent, inst: Instrument, mid: float,
        size: float, positions: dict[str, Position],
    ) -> Optional[liquidation.LiquidationEstimate]:
        if intent.reduce_only or intent.stop_px is None:
            return None
        existing = positions.get(intent.coin)
        known_liq = existing.liquidation_px if existing and not existing.is_flat else None
        return liquidation.assess(
            entry_px=intent.limit_px or mid,
            stop_px=intent.stop_px,
            size=size,
            side=intent.side,
            margin_available=self.equity_usd,
            max_leverage=max(1, inst.max_leverage),
            min_buffer_mult=self.cfg.min_liquidation_buffer_mult,
            known_liquidation_px=known_liq,
        )
