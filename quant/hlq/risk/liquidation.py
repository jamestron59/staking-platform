"""Distance to liquidation.

The distinction this module enforces: a *stop* is where you choose to exit; a
*liquidation* is where the exchange exits you, at a worse price, with a penalty,
and with no regard for whether the move was a one-second wick. Sizing against
the stop while ignoring the liquidation price is how a 0.5%-risk-per-trade rule
produces a 40% loss.

Two sources of truth, used differently:

  - **Pre-trade**: the position does not exist yet, so we model the liquidation
    price with HL's formula to decide whether the size is survivable.
  - **Live**: `clearinghouseState` reports HL's own `liquidationPx` per
    position. That number is authoritative and is what the kill switch reads.
    Our model is never used to override it — models of exchange internals drift
    silently as the exchange changes them.

HL's formula (perps):

    liq_px = px - side * margin_available / (size * (1 - l * side))
    l = 1 / (2 * max_leverage)     # maintenance leverage is 2x max leverage

where `side` is +1 long / -1 short, and `margin_available` is account value
minus maintenance margin (cross) or isolated margin minus maintenance (isolated).
Treat the output as an estimate with a safety buffer, not a precise level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..types import Side


@dataclass(frozen=True, slots=True)
class LiquidationEstimate:
    liquidation_px: Optional[float]
    distance_pct: Optional[float]
    stop_distance_pct: Optional[float]
    buffer_multiple: Optional[float]  # how many stop-distances until liquidation
    survivable: bool
    note: str = ""


def maintenance_margin_fraction(max_leverage: int) -> float:
    """HL sets maintenance margin at half the initial margin at max leverage."""
    if max_leverage <= 0:
        raise ValueError("max_leverage must be positive")
    return 1.0 / (2.0 * max_leverage)


def estimate_liquidation_px(
    *,
    entry_px: float,
    size: float,
    side: Side,
    margin_available: float,
    max_leverage: int,
) -> Optional[float]:
    """None means the inputs are unusable, NOT that the position is safe.

    A long whose computed liquidation price is negative cannot be liquidated by
    any positive price — it is over-collateralised, the safest possible state.
    Conflating that with "cannot determine" would make the risk engine refuse
    precisely the trades that carry least liquidation risk, so the two cases
    are kept distinct: callers use `is_unliquidatable` for the first.
    """
    if size <= 0 or entry_px <= 0:
        return None
    l = maintenance_margin_fraction(max_leverage)
    denom = size * (1.0 - l * side.sign)
    if abs(denom) < 1e-12:
        return None
    return entry_px - side.sign * margin_available / denom


def is_unliquidatable(liq_px: Optional[float], side: Side) -> bool:
    """A long with a non-positive liquidation price can never be liquidated.

    Shorts have no equivalent: their liquidation price is above entry and any
    finite positive value is reachable.
    """
    return liq_px is not None and side is Side.BUY and liq_px <= 0


def assess(
    *,
    entry_px: float,
    stop_px: Optional[float],
    size: float,
    side: Side,
    margin_available: float,
    max_leverage: int,
    min_buffer_mult: float,
    known_liquidation_px: Optional[float] = None,
) -> LiquidationEstimate:
    """`known_liquidation_px` (from the exchange) always wins when present."""
    if entry_px <= 0:
        return LiquidationEstimate(None, None, None, None, False, "invalid entry price")

    # A stop on the wrong side of entry is a configuration error and is checked
    # before anything else — it is wrong regardless of the liquidation maths.
    if stop_px is not None:
        if (side is Side.BUY and stop_px >= entry_px) or (side is Side.SELL and stop_px <= entry_px):
            return LiquidationEstimate(
                None, None, None, None, False, "stop is on the wrong side of entry"
            )

    liq = known_liquidation_px if known_liquidation_px is not None else estimate_liquidation_px(
        entry_px=entry_px, size=size, side=side,
        margin_available=margin_available, max_leverage=max_leverage,
    )

    if is_unliquidatable(liq, side):
        return LiquidationEstimate(
            None, None, None, None, True,
            "over-collateralised: no positive price liquidates this position",
        )
    if liq is None or liq <= 0:
        return LiquidationEstimate(None, None, None, None, False, "cannot determine liquidation price")

    dist_pct = abs(entry_px - liq) / entry_px * 100
    if stop_px is None:
        return LiquidationEstimate(liq, dist_pct, None, None, True, "no stop set: buffer unknown")

    stop_pct = abs(entry_px - stop_px) / entry_px * 100
    if stop_pct <= 0:
        return LiquidationEstimate(liq, dist_pct, 0.0, None, False, "degenerate stop at entry")

    buffer = dist_pct / stop_pct
    survivable = buffer >= min_buffer_mult
    note = "" if survivable else (
        f"stop is only {buffer:.2f}x from liquidation (need {min_buffer_mult}x): "
        f"a wick past the stop liquidates before the stop fills"
    )
    return LiquidationEstimate(liq, dist_pct, stop_pct, buffer, survivable, note)


def max_size_for_buffer(
    *,
    entry_px: float,
    stop_px: float,
    side: Side,
    equity: float,
    max_leverage: int,
    min_buffer_mult: float,
) -> float:
    """Largest size whose liquidation price stays `min_buffer_mult` stop-widths
    away. Solved by bisection because the relationship runs through
    `margin_available`, which itself depends on size.
    """
    if entry_px <= 0 or equity <= 0:
        return 0.0
    stop_dist = abs(entry_px - stop_px)
    if stop_dist <= 0:
        return 0.0
    required_dist = stop_dist * min_buffer_mult

    def ok(size: float) -> bool:
        margin = equity - (size * entry_px) / max_leverage * maintenance_margin_fraction(max_leverage) * 2
        liq = estimate_liquidation_px(
            entry_px=entry_px, size=size, side=side,
            margin_available=max(0.0, margin), max_leverage=max_leverage,
        )
        return liq is not None and abs(entry_px - liq) >= required_dist

    lo, hi = 0.0, equity * max_leverage / entry_px
    if not ok(hi * 1e-6):
        return 0.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo
