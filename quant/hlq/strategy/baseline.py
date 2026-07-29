"""The Phase-2 plumbing strategy.

This exists to be traded with real money at trivial size, before any model
exists. Its purpose is not profit — it is to prove that data capture, feature
computation, risk sizing, order routing, fill handling, reconciliation, state
persistence and the kill switches all work against the live exchange.

Running a sophisticated model through untested plumbing means that when
something goes wrong you cannot tell whether the model is bad or the pipe is
leaking. Running a deliberately simple rule first separates those questions.

The rule itself is order-flow mean reversion: when flow is heavily one-sided
and the book is thin on that side, lean against it. It is a real phenomenon and
it is also nearly always too small to trade after costs — which is the point.
The risk engine's cost gate should reject most of these signals, and watching
it do so is itself the test.

Expect this to be roughly break-even minus costs. If it is dramatically worse,
the problem is in the plumbing, and that is exactly what you wanted to find out
at $20 a trade rather than at full size.
"""

from __future__ import annotations

from typing import Optional

from ..features.pipeline import FeatureVector
from ..logging_setup import get_logger
from ..types import BookSnapshot, OrderIntent, PerpContext, Position, Side
from .base import Strategy

log = get_logger(__name__)


class FlowReversionBaseline(Strategy):
    name = "flow_reversion_baseline"

    def __init__(
        self,
        *,
        flow_threshold: float = 0.35,
        imbalance_threshold: float = 0.15,
        target_notional_usd: float = 25.0,
        stop_vol_mult: float = 1.2,
        target_vol_mult: float = 1.8,
        min_vol: float = 1e-5,
    ) -> None:
        self.flow_threshold = flow_threshold
        self.imbalance_threshold = imbalance_threshold
        self.target_notional_usd = target_notional_usd
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.min_vol = min_vol

    def on_features(
        self,
        *,
        features: FeatureVector,
        book: BookSnapshot,
        position: Optional[Position],
        ctx: Optional[PerpContext],
        now_ms: int,
    ) -> Optional[OrderIntent]:
        if not features.complete:
            return None
        if position is not None and not position.is_flat:
            return None  # one position per coin; exits are managed by the supervisor

        v = features.values
        flow = v.get("flow_imbalance")
        imbalance = v.get("book_imbalance")
        vol = v.get("realised_vol")
        mid = book.mid
        if flow is None or imbalance is None or vol is None or mid is None:
            return None
        if vol < self.min_vol:
            return None  # a dead market: any signal here is rounding error

        # Lean against one-sided flow, but only when the book agrees that the
        # aggressive side has exhausted itself.
        if flow > self.flow_threshold and imbalance < -self.imbalance_threshold:
            side = Side.SELL
        elif flow < -self.flow_threshold and imbalance > self.imbalance_threshold:
            side = Side.BUY
        else:
            return None

        stop_dist = mid * vol * self.stop_vol_mult
        target_dist = mid * vol * self.target_vol_mult
        if stop_dist <= 0:
            return None

        return OrderIntent(
            coin=features.coin,
            side=side,
            target_notional_usd=self.target_notional_usd,
            limit_px=mid,
            stop_px=mid - side.sign * stop_dist,
            take_profit_px=mid + side.sign * target_dist,
            # Stated honestly, in the same units risk uses. This is a weak edge
            # and the number says so: the cost gate will reject most of them.
            expected_edge_bps=1e4 * vol * self.target_vol_mult * 0.35,
            confidence=min(0.6, abs(flow)),
            reason=f"flow_reversion flow={flow:.3f} imb={imbalance:.3f} vol={vol:.5f}",
            meta={"flow": flow, "imbalance": imbalance, "vol": vol},
        )

    def min_interval_ms(self) -> int:
        return 15_000
