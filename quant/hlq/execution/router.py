"""Order routing: turning an approved size into fills without giving it back.

Two things this module deliberately does NOT do, both of which were in the
original design:

**Fixed entry tranches (30/50/20).** Scaling into a position on a fixed
schedule is a discretionary-trading habit. If the signal is right, adding as
price moves away from you worsens the average entry; it only helps when entry
timing is poor, in which case the fix belongs in the signal. What *does*
justify slicing is market impact — so slicing here is driven by size relative
to book depth, and a size that fits in the book is sent in one order.

**Assuming the intended size is the filled size.** The most dangerous moment in
execution is a partial fill: risk was sized for 3 units, 1 filled, and the stop
is still placed for 3. `FillTracker` makes the filled size the only number that
downstream risk ever sees.

The maker-first path is a real optimisation on HL, where the maker/taker spread
is several bps — but it is time-boxed. An unfilled maker order is not free: the
market moved, and the trades you miss are the good ones. After
`maker_timeout_ms` we requote a bounded number of times, then either cross or
abandon, never chase indefinitely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import ExecutionConfig
from ..costs import CostModel
from ..instruments import Instrument, Rounding
from ..logging_setup import get_logger
from ..types import BookSnapshot, Fill, Order, OrderStatus, Side, TimeInForce
from .gateway import Gateway, SubmitResult, new_cloid

log = get_logger(__name__)


@dataclass
class Slice:
    size: float
    reason: str


@dataclass
class ExecutionReport:
    """What actually happened, as opposed to what was requested."""

    requested_size: float
    filled_size: float = 0.0
    avg_px: float = 0.0
    fees: float = 0.0
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    requotes: int = 0
    crossed: bool = False
    aborted_reason: str = ""

    @property
    def fill_ratio(self) -> float:
        return self.filled_size / self.requested_size if self.requested_size > 0 else 0.0

    @property
    def is_partial(self) -> bool:
        return 0 < self.fill_ratio < 0.999

    def record(self, fill: Fill) -> None:
        total = self.filled_size + fill.sz
        if total > 0:
            self.avg_px = (self.avg_px * self.filled_size + fill.px * fill.sz) / total
        self.filled_size = total
        self.fees += fill.fee
        self.fills.append(fill)
        self.crossed = self.crossed or fill.crossed


class Router:
    def __init__(self, cfg: ExecutionConfig, costs: CostModel) -> None:
        self.cfg = cfg
        self.costs = costs

    # ---- slicing ----------------------------------------------------------

    def plan_slices(self, size: float, book: BookSnapshot, side: Side) -> list[Slice]:
        """Slice only when impact demands it.

        The threshold is our size against visible depth on the side we will
        consume. Below it, one order: splitting a small order into five just
        pays the spread five times and leaks intent.
        """
        available = book.depth(side, 10.0)
        if available <= 0:
            return [Slice(size, "no_depth_visible")]
        share = size / available
        if share <= self.cfg.max_participation_of_depth:
            return [Slice(size, "fits_in_book")]
        n = min(self.cfg.max_slices, max(2, int(share / self.cfg.max_participation_of_depth) + 1))
        per = size / n
        return [Slice(per, f"impact_slice_{i+1}_of_{n}") for i in range(n)]

    # ---- execution --------------------------------------------------------

    async def execute(
        self,
        *,
        gateway: Gateway,
        instrument: Instrument,
        side: Side,
        size: float,
        book: BookSnapshot,
        reduce_only: bool = False,
        on_order_created=None,
    ) -> ExecutionReport:
        report = ExecutionReport(requested_size=size)
        slices = self.plan_slices(size, book, side)
        if len(slices) > 1:
            log.event("slicing", coin=instrument.name, slices=len(slices),
                      total=size, reason=slices[0].reason)

        for idx, sl in enumerate(slices):
            if idx > 0:
                import asyncio
                await asyncio.sleep(self.cfg.slice_interval_ms / 1000)
            remaining = instrument.round_size(sl.size)
            if remaining <= 0:
                continue
            await self._execute_one(
                gateway=gateway, instrument=instrument, side=side, size=remaining,
                book=book, reduce_only=reduce_only, report=report,
                on_order_created=on_order_created,
            )
            if report.aborted_reason:
                break
        return report

    async def _execute_one(
        self, *, gateway: Gateway, instrument: Instrument, side: Side, size: float,
        book: BookSnapshot, reduce_only: bool, report: ExecutionReport, on_order_created,
    ) -> None:
        import asyncio

        remaining = size
        for attempt in range(self.cfg.max_requotes + 1):
            if remaining <= 0:
                return
            is_last = attempt == self.cfg.max_requotes
            use_maker = self.cfg.prefer_maker and not is_last

            px = self._quote_price(instrument, side, book, maker=use_maker)
            if px is None:
                report.aborted_reason = "no_quotable_price"
                return
            sz = instrument.round_size(remaining)
            if sz <= 0 or not instrument.meets_min_notional(px, sz):
                # A leftover too small to send is not an error; it is the lot
                # size doing its job. Report what filled and stop.
                return

            order = Order(
                cloid=new_cloid(), coin=instrument.name, side=side, sz=sz, limit_px=px,
                tif=TimeInForce.ALO if use_maker else TimeInForce.IOC,
                reduce_only=reduce_only, created_ms=int(time.time() * 1000),
            )
            if on_order_created:
                on_order_created(order, book)
            report.orders.append(order)

            result = await gateway.submit(order)

            if result.indeterminate:
                # Never re-send. Resolve first — the order may be live.
                resolved = await self._resolve(gateway, order)
                if resolved is None:
                    report.aborted_reason = "indeterminate_unresolved"
                    return
                order = resolved

            if result.immediate_fill:
                report.record(result.immediate_fill)
                remaining -= result.immediate_fill.sz
                continue

            if not result.ok:
                if "post_only" in result.error or "would cross" in result.error.lower():
                    # Expected on a fast market: the book moved between the
                    # snapshot and the send. Requote rather than treating it as
                    # a failure.
                    report.requotes += 1
                    log.debug("post_only_rejected_requoting", cloid=order.cloid, attempt=attempt)
                    await asyncio.sleep(0.05)
                    continue
                report.aborted_reason = result.error or "submit_failed"
                return

            filled = await self._await_fill_or_timeout(gateway, order)
            for f in filled:
                report.record(f)
                remaining -= f.sz
            if remaining > 1e-12 and not is_last:
                await gateway.cancel(order.cloid, order.coin)
                report.requotes += 1

        if remaining > 1e-12:
            log.warn("execution_incomplete", coin=instrument.name,
                     requested=size, filled=size - remaining)

    def _quote_price(
        self, instrument: Instrument, side: Side, book: BookSnapshot, *, maker: bool
    ) -> Optional[float]:
        if maker:
            # Join the near touch, rounded so we stay passive. Rounding the
            # wrong way here converts a rebate into a taker fee.
            ref = book.best_bid if side is Side.BUY else book.best_ask
            if ref is None:
                return None
            return instrument.round_price(ref, Rounding.passive_for(side))
        # Marketable limit, never a true market order: the limit caps how far
        # a thin book can carry us.
        mid = book.mid
        if mid is None:
            return None
        capped = mid * (1 + side.sign * self.cfg.ioc_slippage_bps / 1e4)
        return instrument.round_price(capped, Rounding.aggressive_for(side))

    async def _resolve(self, gateway: Gateway, order: Order) -> Optional[Order]:
        resolver = getattr(gateway, "resolve_indeterminate", None)
        if resolver is None:
            return await gateway.query_by_cloid(order.cloid)
        return await resolver(order)

    async def _await_fill_or_timeout(self, gateway: Gateway, order: Order) -> list[Fill]:
        """Poll the order until it fills, cancels, or the maker window closes.

        Polling rather than relying purely on the websocket fill feed is
        deliberate: the feed is the fast path, this is the correctness path,
        and a fill we never learn about is a position we do not know we hold.
        """
        import asyncio

        deadline = time.time() + self.cfg.maker_timeout_ms / 1000
        fills: list[Fill] = []
        while time.time() < deadline:
            await asyncio.sleep(0.15)
            current = await gateway.query_by_cloid(order.cloid)
            if current is None:
                continue
            newly = current.filled_sz - order.filled_sz
            if newly > 1e-12:
                fills.append(Fill(
                    coin=order.coin, exchange_ms=int(time.time() * 1000),
                    px=current.avg_fill_px or order.limit_px, sz=newly, side=order.side,
                    fee=0.0, oid=current.oid or 0, tid=0,
                    crossed=order.tif is TimeInForce.IOC, cloid=order.cloid,
                ))
                order.filled_sz = current.filled_sz
            if current.status.is_terminal:
                break
        return fills


class FillTracker:
    """Keeps intended and actual size apart, permanently.

    The failure this prevents: risk approves 3 units with a stop 2% away,
    execution fills 1, and a stop order for 3 units goes to the exchange. The
    position is now 1 long with a 3-unit reduce-only stop — which on fill
    flips the position short. `protective_size` is the only size a stop should
    ever be built from.
    """

    def __init__(self) -> None:
        self._filled: dict[str, float] = {}

    def on_fill(self, fill: Fill) -> float:
        self._filled[fill.coin] = self._filled.get(fill.coin, 0.0) + fill.sz * fill.side.sign
        return self._filled[fill.coin]

    def protective_size(self, coin: str) -> float:
        return abs(self._filled.get(coin, 0.0))

    def net_position(self, coin: str) -> float:
        return self._filled.get(coin, 0.0)

    def reset(self, coin: str) -> None:
        self._filled.pop(coin, None)

    def sync(self, coin: str, exchange_size: float) -> None:
        """Exchange truth overrides local accounting, always."""
        self._filled[coin] = exchange_size
