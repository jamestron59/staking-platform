"""Tick/lot arithmetic for Hyperliquid.

This is the least glamorous file in the repo and one of the two or three where
a bug costs real money immediately. Two independent rules apply to a perp price
and BOTH must hold:

  1. at most 5 significant figures;
  2. at most (6 - szDecimals) decimal places  (8 - szDecimals for spot).

Integer prices are always accepted regardless of rule 1 — that is what makes
BTC at 118_432 legal even though it is 6 significant figures.

Verified against the official SDK: `Exchange._slippage_price` implements exactly
`round(float(f"{px:.5g}"), (6 if perp else 8) - szDecimals)`, and
`signing.float_to_wire` *raises* rather than silently rounding, so an unrounded
price is a hard order rejection, not a slightly-off fill.

Direction matters as much as precision. Rounding a post-only buy *up* can push
it across the spread, where it is rejected (Alo) or pays taker fees. So every
rounding call site must state its intent, and `round_price` has no default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from typing import Any, Iterable, Mapping

from .types import Side

PERP_MAX_DECIMALS = 6
SPOT_MAX_DECIMALS = 8
MAX_SIG_FIGS = 5
WIRE_MAX_DECIMALS = 8  # signing.float_to_wire formats with %.8f and rejects loss


class Rounding(str, Enum):
    DOWN = "down"
    UP = "up"
    NEAREST = "nearest"

    @staticmethod
    def passive_for(side: Side) -> "Rounding":
        """Rounding that keeps a resting order passive: a buy moves down, a
        sell moves up. Never crosses the spread by accident."""
        return Rounding.DOWN if side is Side.BUY else Rounding.UP

    @staticmethod
    def aggressive_for(side: Side) -> "Rounding":
        return Rounding.UP if side is Side.BUY else Rounding.DOWN


_DECIMAL_MODE = {
    Rounding.DOWN: ROUND_FLOOR,
    Rounding.UP: ROUND_CEILING,
    Rounding.NEAREST: ROUND_HALF_EVEN,
}


@dataclass(frozen=True, slots=True)
class Instrument:
    name: str
    asset_id: int
    sz_decimals: int
    is_spot: bool = False
    min_notional_usd: float = 10.0
    max_leverage: int = 1
    # The integer exemption is what lets BTC quote on a $1 tick instead of $10.
    # The SDK's `_slippage_price` does NOT apply it (it hard-truncates to 5
    # significant figures), which is safe for a slippage cap but leaves ~$3 per
    # BTC order on the table for a resting quote. We apply it, and pay for that
    # precision with a small rejection risk if HL ever tightens the rule — which
    # the reject-rate kill switch surfaces immediately and loudly. Set
    # `strict_sig_figs=True` to fall back to exact SDK parity.
    strict_sig_figs: bool = False

    @property
    def max_price_decimals(self) -> int:
        base = SPOT_MAX_DECIMALS if self.is_spot else PERP_MAX_DECIMALS
        return max(0, base - self.sz_decimals)

    @property
    def size_step(self) -> float:
        return 10.0 ** (-self.sz_decimals)

    def price_decimals_for(self, px: float) -> int:
        """Effective decimal places allowed at this price level.

        Combines both constraints. `Decimal.adjusted()` gives floor(log10(px)),
        so a price in [1, 10) has adjusted()==0 and may carry 4 decimals, which
        is 5 significant figures. When the significant-figure rule would demand
        *negative* decimals (px >= 100_000) we clamp to 0, relying on the
        integer exemption.
        """
        if px <= 0 or not math.isfinite(px):
            raise ValueError(f"non-positive price {px}")
        exponent = Decimal(repr(px)).adjusted()
        sig_decimals = MAX_SIG_FIGS - 1 - exponent
        if self.strict_sig_figs:
            return min(self.max_price_decimals, sig_decimals)
        return max(0, min(self.max_price_decimals, sig_decimals))

    def round_price(self, px: float, rounding: Rounding) -> float:
        """Snap `px` to a legal price. `rounding` is mandatory by design."""
        d = Decimal(repr(px))
        decimals = self.price_decimals_for(px)
        with localcontext() as ctx:
            ctx.prec = 28
            # `decimals` may be negative in strict mode (quantise to tens/hundreds).
            quantum = Decimal(1).scaleb(-decimals)
            snapped = d.quantize(quantum, rounding=_DECIMAL_MODE[rounding])
        if snapped <= 0:
            # Reaching here means the price is orders of magnitude away from what
            # this instrument's szDecimals implies — a unit-conversion bug or a
            # corrupt feed. Substituting a "reasonable" tick would turn a caught
            # bug into a live order at an absurd price. Fail loudly instead.
            raise ValueError(
                f"{self.name}: price {px!r} rounds to {snapped} at {decimals} decimals "
                f"(szDecimals={self.sz_decimals}); refusing to fabricate a price"
            )
        return float(snapped)

    def round_size(self, sz: float) -> float:
        """Sizes always round toward zero: we may take less risk than intended,
        never more."""
        d = Decimal(repr(abs(sz)))
        quantum = Decimal(1).scaleb(-self.sz_decimals)
        snapped = d.quantize(quantum, rounding=ROUND_FLOOR)
        return float(snapped) * (1.0 if sz >= 0 else -1.0)

    def is_valid_price(self, px: float) -> bool:
        if px <= 0 or not math.isfinite(px):
            return False
        d = Decimal(repr(px)).normalize()
        decimals = max(0, -d.as_tuple().exponent)
        if decimals > self.max_price_decimals or decimals > WIRE_MAX_DECIMALS:
            return False
        if d == d.to_integral_value():
            return True  # integer exemption
        return len(d.as_tuple().digits) <= MAX_SIG_FIGS

    def is_valid_size(self, sz: float) -> bool:
        if sz <= 0 or not math.isfinite(sz):
            return False
        d = Decimal(repr(sz)).normalize()
        return max(0, -d.as_tuple().exponent) <= self.sz_decimals

    def meets_min_notional(self, px: float, sz: float) -> bool:
        return px * sz >= self.min_notional_usd - 1e-9

    def size_for_notional(self, notional_usd: float, px: float) -> float:
        if px <= 0:
            raise ValueError("price must be positive")
        return self.round_size(notional_usd / px)


class InstrumentRegistry:
    """Built once at startup from the HL `meta` response, then read-only.

    Refusing to trade an unknown symbol is intentional: a typo in a config file
    must fail loudly at boot, not resolve to a default at 3am.
    """

    def __init__(self, instruments: Iterable[Instrument]) -> None:
        self._by_name = {i.name: i for i in instruments}

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any]) -> "InstrumentRegistry":
        """`meta` is the payload of the HL info request {"type": "meta"}.

        Asset id is the index into `universe`, which is exactly how the SDK's
        `name_to_asset` resolves it.
        """
        out = []
        for idx, asset in enumerate(meta["universe"]):
            out.append(
                Instrument(
                    name=asset["name"],
                    asset_id=idx,
                    sz_decimals=int(asset["szDecimals"]),
                    is_spot=False,
                    max_leverage=int(asset.get("maxLeverage", 1)),
                )
            )
        return cls(out)

    def get(self, name: str) -> Instrument:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(
                f"unknown instrument {name!r}; known: {sorted(self._by_name)[:20]}..."
            ) from None

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def names(self) -> list[str]:
        return sorted(self._by_name)
