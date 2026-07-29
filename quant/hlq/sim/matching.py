"""Fill simulation.

Backtests lie in a predictable direction, and almost all of the lie lives here.
Three specific optimisms this module refuses:

  1. **Maker orders always fill.** In reality a resting bid fills when the
     market comes to you — which is disproportionately when you were wrong.
     We model queue position: on placement you join the BACK of the queue at
     your price level, and you only fill once the size that was ahead of you
     has traded away.

  2. **Fills happen at the decision price.** We apply a latency delay between
     the decision and the order becoming live, so the book you decided on is
     not the book you trade against.

  3. **Unlimited size at the touch.** Taker orders sweep real levels via
     `walk_book`, and a size the book cannot absorb is rejected rather than
     silently filled.

The queue model is still optimistic in one respect worth stating: it assumes no
one cancels ahead of you, which in practice they do, so real queue position
improves faster than modelled. It is deliberately biased against the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..data.book import walk_book
from ..types import BookSnapshot, Fill, Order, OrderStatus, Side, TimeInForce, Trade


@dataclass(slots=True)
class RestingOrder:
    order: Order
    queue_ahead: float  # size at our level that must trade before we do
    live_from_ms: int  # decision time + latency
    placed_mid: float


class MatchingEngine:
    """Simulates HL's book from our order's point of view.

    Not a full exchange simulator — it does not model other participants
    reacting to us. For the sizes this system trades (hundreds of dollars
    against books with tens of thousands) that assumption holds. It stops
    holding the moment size becomes a meaningful share of depth, which is
    exactly what `max_participation_of_depth` in the router prevents.
    """

    def __init__(self, *, taker_fee_bps: float, maker_fee_bps: float, latency_ms: int = 145) -> None:
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.latency_ms = latency_ms
        self._resting: dict[str, RestingOrder] = {}
        self._next_oid = 1
        self.rejected: list[tuple[str, str]] = []

    # ---- submission -------------------------------------------------------

    def submit(
        self, order: Order, book: BookSnapshot, now_ms: int
    ) -> tuple[Optional[Fill], Optional[str]]:
        """Returns (immediate_fill, rejection_reason)."""
        live_from = now_ms + self.latency_ms
        crosses = self._crosses(order, book)

        if order.tif is TimeInForce.ALO:
            if crosses:
                # Post-only that would cross is rejected by HL, not repriced.
                # Modelling it as a taker fill would invent fills that never
                # existed and understate how often the maker path fails.
                order.status = OrderStatus.REJECTED
                order.error = "post_only_would_cross"
                self.rejected.append((order.cloid, order.error))
                return None, order.error
            self._rest(order, book, live_from)
            return None, None

        if order.tif is TimeInForce.IOC:
            return self._take(order, book, now_ms), None

        # GTC: cross what we can now, rest the remainder.
        if crosses:
            fill = self._take(order, book, now_ms)
            if order.remaining > 1e-12:
                self._rest(order, book, live_from)
            return fill, None
        self._rest(order, book, live_from)
        return None, None

    @staticmethod
    def _crosses(order: Order, book: BookSnapshot) -> bool:
        if order.side is Side.BUY:
            return book.best_ask is not None and order.limit_px >= book.best_ask
        return book.best_bid is not None and order.limit_px <= book.best_bid

    def _rest(self, order: Order, book: BookSnapshot, live_from_ms: int) -> None:
        levels = book.bids if order.side is Side.BUY else book.asks
        ahead = next((lv.sz for lv in levels if abs(lv.px - order.limit_px) < 1e-12), 0.0)
        order.status = OrderStatus.OPEN
        order.oid = self._next_oid
        self._next_oid += 1
        self._resting[order.cloid] = RestingOrder(
            order=order,
            queue_ahead=ahead,
            live_from_ms=live_from_ms,
            placed_mid=book.mid or order.limit_px,
        )

    def _take(self, order: Order, book: BookSnapshot, now_ms: int) -> Optional[Fill]:
        avg_px, filled, exhausted = walk_book(book, order.side, order.remaining)
        if filled <= 0:
            order.status = OrderStatus.REJECTED
            order.error = "no_liquidity"
            self.rejected.append((order.cloid, order.error))
            return None
        # Respect the limit: we never fill through our own price.
        if (order.side is Side.BUY and avg_px > order.limit_px) or (
            order.side is Side.SELL and avg_px < order.limit_px
        ):
            avg_px, filled, _ = self._fill_to_limit(book, order.side, order.remaining, order.limit_px)
            if filled <= 0:
                order.status = OrderStatus.CANCELLED
                order.error = "ioc_unfilled_limit"
                return None
        fee = avg_px * filled * self.taker_fee_bps / 1e4
        order.apply_fill(avg_px, filled, now_ms)
        return Fill(
            coin=order.coin, exchange_ms=now_ms, px=avg_px, sz=filled, side=order.side,
            fee=fee, oid=order.oid or 0, tid=0, crossed=True, cloid=order.cloid,
        )

    @staticmethod
    def _fill_to_limit(
        book: BookSnapshot, side: Side, size: float, limit_px: float
    ) -> tuple[float, float, bool]:
        levels = book.asks if side is Side.BUY else book.bids
        remaining, notional = size, 0.0
        for lv in levels:
            if (side is Side.BUY and lv.px > limit_px) or (side is Side.SELL and lv.px < limit_px):
                break
            take = min(remaining, lv.sz)
            notional += take * lv.px
            remaining -= take
            if remaining <= 1e-12:
                break
        filled = size - remaining
        return (notional / filled if filled > 0 else 0.0), filled, remaining > 1e-12

    # ---- resting order lifecycle -----------------------------------------

    def on_trade(self, trade: Trade, now_ms: int) -> list[Fill]:
        """A print consumes queue. Only trades on the opposite aggressor side
        can fill us: our resting bid is hit by a seller."""
        fills: list[Fill] = []
        for cloid in list(self._resting):
            ro = self._resting.get(cloid)
            if ro is None or ro.order.coin != trade.coin or now_ms < ro.live_from_ms:
                continue
            o = ro.order
            if o.side is Side.BUY and trade.aggressor is not Side.SELL:
                continue
            if o.side is Side.SELL and trade.aggressor is not Side.BUY:
                continue

            if (o.side is Side.BUY and trade.px < o.limit_px) or (
                o.side is Side.SELL and trade.px > o.limit_px
            ):
                # Price traded through us: everything ahead is gone, we fill.
                fills.append(self._fill_resting(ro, o.remaining, now_ms))
            elif abs(trade.px - o.limit_px) < 1e-12:
                consumed = trade.sz
                if ro.queue_ahead > 0:
                    eaten = min(ro.queue_ahead, consumed)
                    ro.queue_ahead -= eaten
                    consumed -= eaten
                if consumed > 1e-12:
                    fills.append(self._fill_resting(ro, min(consumed, o.remaining), now_ms))
            if ro.order.status is OrderStatus.FILLED:
                self._resting.pop(cloid, None)
        return [f for f in fills if f is not None]

    def _fill_resting(self, ro: RestingOrder, sz: float, now_ms: int) -> Fill:
        o = ro.order
        sz = min(sz, o.remaining)
        fee = o.limit_px * sz * self.maker_fee_bps / 1e4
        o.apply_fill(o.limit_px, sz, now_ms)
        return Fill(
            coin=o.coin, exchange_ms=now_ms, px=o.limit_px, sz=sz, side=o.side,
            fee=fee, oid=o.oid or 0, tid=0, crossed=False, cloid=o.cloid,
        )

    def cancel(self, cloid: str, now_ms: int) -> bool:
        ro = self._resting.pop(cloid, None)
        if ro is None:
            return False
        ro.order.status = OrderStatus.CANCELLED
        ro.order.last_update_ms = now_ms
        return True

    def cancel_all(self, now_ms: int) -> int:
        n = len(self._resting)
        for cloid in list(self._resting):
            self.cancel(cloid, now_ms)
        return n

    def open_orders(self) -> list[Order]:
        return [ro.order for ro in self._resting.values()]
