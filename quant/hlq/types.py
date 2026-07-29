"""Core domain types.

Events are frozen: once observed, a market fact never changes. Anything mutable
(positions, order state) lives in `ops.state` and is persisted explicitly.

Timestamps are integer milliseconds UTC throughout. Two distinct clocks matter
and are never conflated:
  - `exchange_ms`: the timestamp HL stamped on the event. Used for all feature
    computation and labelling, so backtest and live agree.
  - `local_ms`: when we received it. Used only for staleness/latency monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @staticmethod
    def from_hl_aggressor(raw: str) -> "Side":
        """HL trade feed marks the *aggressor* side as 'A' or 'B'.

        'B' = the taker bought (lifted the ask), 'A' = the taker sold (hit the bid).
        This mapping is asserted against live data by `hlq.data.recorder`'s
        sanity check: buy-aggressor prints should cluster at or above the mid.
        """
        if raw == "B":
            return Side.BUY
        if raw == "A":
            return Side.SELL
        raise ValueError(f"unknown HL side {raw!r}")


class TimeInForce(str, Enum):
    ALO = "Alo"  # post-only: rejected if it would cross
    IOC = "Ioc"
    GTC = "Gtc"


class OrderStatus(str, Enum):
    PENDING = "pending"  # submitted, no exchange ack yet — the dangerous state
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"  # ack lost; must be resolved by the reconciler

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True, slots=True)
class Level:
    px: float
    sz: float
    n: int = 0


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    coin: str
    exchange_ms: int
    bids: Sequence[Level]
    asks: Sequence[Level]
    local_ms: int = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].px if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].px + self.asks[0].px) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return self.asks[0].px - self.bids[0].px

    @property
    def spread_bps(self) -> Optional[float]:
        mid, sp = self.mid, self.spread
        if mid is None or sp is None or mid <= 0:
            return None
        return 1e4 * sp / mid

    def microprice(self) -> Optional[float]:
        """Size-weighted mid. Leans toward the side with less resting size,
        which is the side price is more likely to move toward."""
        if not self.bids or not self.asks:
            return None
        bz, az = self.bids[0].sz, self.asks[0].sz
        if bz + az <= 0:
            return self.mid
        return (self.bids[0].px * az + self.asks[0].px * bz) / (bz + az)

    def depth(self, side: Side, bps: float) -> float:
        """Resting size within `bps` of the mid on `side`. The denominator for
        every impact estimate we make."""
        mid = self.mid
        if mid is None:
            return 0.0
        levels = self.bids if side is Side.BUY else self.asks
        limit = mid * (1 - bps / 1e4) if side is Side.BUY else mid * (1 + bps / 1e4)
        total = 0.0
        for lv in levels:
            if (side is Side.BUY and lv.px < limit) or (side is Side.SELL and lv.px > limit):
                break
            total += lv.sz
        return total


@dataclass(frozen=True, slots=True)
class Trade:
    coin: str
    exchange_ms: int
    px: float
    sz: float
    aggressor: Side
    tid: int = 0
    local_ms: int = 0

    @property
    def signed_sz(self) -> float:
        return self.sz * self.aggressor.sign


@dataclass(frozen=True, slots=True)
class PerpContext:
    """From the `activeAssetCtx` subscription — the perp-specific state that
    generic TA ignores and that actually carries information."""

    coin: str
    exchange_ms: int
    mark_px: float
    oracle_px: float
    funding: float  # per-hour rate, as a decimal
    open_interest: float
    premium: float
    mid_px: Optional[float] = None
    impact_bid: Optional[float] = None
    impact_ask: Optional[float] = None
    local_ms: int = 0


@dataclass(frozen=True, slots=True)
class Fill:
    coin: str
    exchange_ms: int
    px: float
    sz: float
    side: Side
    fee: float
    oid: int
    tid: int
    crossed: bool  # True => we were the taker
    cloid: Optional[str] = None
    closed_pnl: float = 0.0
    start_position: float = 0.0

    @property
    def notional(self) -> float:
        return self.px * self.sz


@dataclass(frozen=True, slots=True)
class Position:
    coin: str
    size: float = 0.0  # signed: >0 long, <0 short
    entry_px: float = 0.0
    leverage: float = 1.0
    liquidation_px: Optional[float] = None
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0

    @property
    def is_flat(self) -> bool:
        return abs(self.size) < 1e-12

    @property
    def side(self) -> Optional[Side]:
        if self.is_flat:
            return None
        return Side.BUY if self.size > 0 else Side.SELL

    def notional(self, px: float) -> float:
        return abs(self.size) * px


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What the strategy wants. Deliberately separate from `Order` (what we
    actually sent): the router may slice one intent into many orders, and risk
    may shrink or veto it."""

    coin: str
    side: Side
    target_notional_usd: float
    limit_px: Optional[float] = None
    reduce_only: bool = False
    stop_px: Optional[float] = None
    take_profit_px: Optional[float] = None
    confidence: float = 0.0
    expected_edge_bps: float = 0.0
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class Order:
    """Mutable: an order's state evolves. `cloid` is the idempotency key and is
    generated *before* the first send attempt, so a retry after a timeout can
    never create a duplicate."""

    cloid: str
    coin: str
    side: Side
    sz: float
    limit_px: float
    tif: TimeInForce
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.PENDING
    oid: Optional[int] = None
    filled_sz: float = 0.0
    avg_fill_px: float = 0.0
    created_ms: int = 0
    last_update_ms: int = 0
    intent_id: str = ""
    error: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self.sz - self.filled_sz)

    def apply_fill(self, px: float, sz: float, ts_ms: int) -> None:
        new_filled = self.filled_sz + sz
        if new_filled > 0:
            self.avg_fill_px = (self.avg_fill_px * self.filled_sz + px * sz) / new_filled
        self.filled_sz = new_filled
        self.last_update_ms = ts_ms
        self.status = OrderStatus.FILLED if self.remaining <= 1e-12 else OrderStatus.PARTIAL
