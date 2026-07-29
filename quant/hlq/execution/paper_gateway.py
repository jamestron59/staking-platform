"""Paper gateway: live data, simulated fills, identical interface.

Implements the same `Gateway` protocol as the live one, so the supervisor,
router, risk engine and strategy run byte-identical code paths. The only
difference is where fills come from.

This is what makes a paper run evidence. A paper mode implemented as
`if paper: pretend_filled()` inside the trading loop proves that the pretend
branch works and nothing else.

The simulated fills come from `sim.matching.MatchingEngine` — the same engine
the backtester uses — so paper results and backtest results are comparable, and
a divergence between them points at the data, not at two different simulators.
"""

from __future__ import annotations

import time
from typing import Optional

from ..costs import CostModel
from ..logging_setup import get_logger
from ..sim.matching import MatchingEngine
from ..types import BookSnapshot, Fill, Order, OrderStatus, Position, Side, Trade
from .gateway import AccountSnapshot, SubmitResult

log = get_logger(__name__)


class PaperGateway:
    def __init__(
        self,
        *,
        starting_equity: float,
        costs: CostModel,
        latency_ms: int = 145,
    ) -> None:
        self.equity = starting_equity
        self.starting_equity = starting_equity
        self.costs = costs
        self.engine = MatchingEngine(
            taker_fee_bps=costs.taker_fee_bps,
            maker_fee_bps=costs.maker_fee_bps,
            latency_ms=latency_ms,
        )
        self._books: dict[str, BookSnapshot] = {}
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._realised_pnl = 0.0
        self._fees_paid = 0.0
        self.fills: list[Fill] = []

    # ---- market data feed -------------------------------------------------

    def on_book(self, book: BookSnapshot) -> None:
        self._books[book.coin] = book
        self._mark_to_market()

    def on_trade(self, trade: Trade, now_ms: Optional[int] = None) -> list[Fill]:
        now = now_ms or int(time.time() * 1000)
        fills = self.engine.on_trade(trade, now)
        for f in fills:
            self._apply_fill(f)
        return fills

    # ---- Gateway protocol -------------------------------------------------

    async def submit(self, order: Order) -> SubmitResult:
        book = self._books.get(order.coin)
        if book is None:
            return SubmitResult(ok=False, cloid=order.cloid, error="no_book_for_coin")
        now = int(time.time() * 1000)
        self._orders[order.cloid] = order
        fill, rejection = self.engine.submit(order, book, now)
        if rejection:
            return SubmitResult(ok=False, cloid=order.cloid, error=rejection)
        if fill:
            self._apply_fill(fill)
            return SubmitResult(ok=True, cloid=order.cloid, oid=order.oid, immediate_fill=fill)
        return SubmitResult(ok=True, cloid=order.cloid, oid=order.oid)

    async def cancel(self, cloid: str, coin: str) -> bool:
        return self.engine.cancel(cloid, int(time.time() * 1000))

    async def cancel_all(
        self, coin: Optional[str] = None, cloids: Optional[set[str]] = None
    ) -> int:
        # The simulated book only ever contains our own orders, so the cloid
        # restriction is a no-op here. The signature matches the live gateway
        # so the supervisor cannot behave differently between the two.
        return self.engine.cancel_all(int(time.time() * 1000))

    async def query_by_cloid(self, cloid: str) -> Optional[Order]:
        return self._orders.get(cloid)

    async def account(self) -> AccountSnapshot:
        self._mark_to_market()
        return AccountSnapshot(
            equity_usd=self.equity,
            positions={c: p for c, p in self._positions.items() if not p.is_flat},
            open_orders=self.engine.open_orders(),
            at_ms=int(time.time() * 1000),
        )

    async def refresh_dead_man_switch(self) -> None:
        return None

    async def close(self) -> None:
        self.engine.cancel_all(int(time.time() * 1000))

    # ---- accounting -------------------------------------------------------

    def _apply_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self._fees_paid += fill.fee
        pos = self._positions.get(fill.coin, Position(coin=fill.coin))
        signed = fill.sz * fill.side.sign
        new_size = pos.size + signed

        if pos.size != 0 and (pos.size > 0) != (signed > 0):
            # Reducing or flipping: realise PnL on the closed portion.
            closed = min(abs(signed), abs(pos.size))
            self._realised_pnl += closed * (fill.px - pos.entry_px) * (1 if pos.size > 0 else -1)
            entry_px = fill.px if abs(signed) > abs(pos.size) else pos.entry_px
        elif abs(new_size) > 1e-12:
            entry_px = (pos.entry_px * abs(pos.size) + fill.px * fill.sz) / abs(new_size) \
                if abs(pos.size) > 1e-12 else fill.px
        else:
            entry_px = 0.0

        self._positions[fill.coin] = Position(
            coin=fill.coin,
            size=new_size if abs(new_size) > 1e-12 else 0.0,
            entry_px=entry_px,
        )
        self._mark_to_market()

    def _mark_to_market(self) -> None:
        unrealised = 0.0
        for coin, pos in self._positions.items():
            book = self._books.get(coin)
            if book is None or pos.is_flat:
                continue
            mid = book.mid
            if mid:
                unrealised += pos.size * (mid - pos.entry_px)
        self.equity = self.starting_equity + self._realised_pnl - self._fees_paid + unrealised

    def stats(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "realised_pnl": round(self._realised_pnl, 2),
            "fees_paid": round(self._fees_paid, 2),
            "n_fills": len(self.fills),
            "rejections": len(self.engine.rejected),
            "open_positions": {c: p.size for c, p in self._positions.items() if not p.is_flat},
        }
