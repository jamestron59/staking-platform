"""The live loop. Paper and live differ only in which gateway is injected.

Order of operations at every decision point, and the order matters:

  1. staleness check   — are we allowed to believe our own data?
  2. kill switch       — are we allowed to trade at all?
  3. features          — what do we know, point-in-time?
  4. strategy          — what does it want?
  5. risk              — how much, if any?
  6. router            — get it done without giving the edge back
  7. journal           — record why, for the model layer and the post-mortem

Steps 1 and 2 come first because everything after them is meaningless if the
data is stale or the account is in trouble. A loop that computes a beautiful
signal on a four-minute-old book has done worse than nothing.

Rejected signals are written to the shadow journal at step 5. That is what
makes it possible, later, to ask whether the filters are earning their keep —
the counterfactual the original auto-learning design had no way to observe.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from ..config import Config, Mode
from ..costs import CostModel, SlippageCalibrator
from ..data.book import parse_book
from ..data.replay import parse_perp_ctx, parse_trades
from ..data.ws import HyperliquidWS, Subscription
from ..execution.gateway import Gateway
from ..execution.router import FillTracker, Router
from ..features.pipeline import FeaturePipeline
from ..instruments import InstrumentRegistry, Rounding
from ..logging_setup import get_logger
from ..risk.engine import RiskEngine
from ..risk.killswitch import KillSwitch, TripReason
from ..strategy.base import Strategy, Throttle
from ..types import BookSnapshot, Fill, OrderIntent, PerpContext, Position, Side, Trade
from .state import OrderRecord, ShadowJournal, StateStore

log = get_logger(__name__)


@dataclass
class OpenTrade:
    coin: str
    side: Side
    entry_px: float
    size: float
    entry_ms: int
    stop_px: Optional[float]
    target_px: Optional[float]
    reason: str
    confidence: float
    expected_edge_bps: float
    features: dict


class Supervisor:
    def __init__(
        self,
        cfg: Config,
        gateway: Gateway,
        strategy: Strategy,
        registry: InstrumentRegistry,
        store: StateStore,
    ) -> None:
        self.cfg = cfg
        self.gateway = gateway
        self.strategy = strategy
        self.registry = registry
        self.store = store
        self.shadow = ShadowJournal(cfg.state_dir)

        self.costs = CostModel(
            taker_fee_bps=cfg.costs.taker_fee_bps,
            maker_fee_bps=cfg.costs.maker_fee_bps,
            residual_slippage_bps=cfg.costs.residual_slippage_bps,
        )
        self.kill = KillSwitch(
            max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
            max_drawdown_pct=cfg.risk.max_drawdown_pct,
            max_consecutive_losses=cfg.risk.max_consecutive_losses,
            max_reject_rate=cfg.risk.max_reject_rate,
            reject_window=cfg.risk.reject_window,
            max_staleness_ms=cfg.data.max_staleness_ms,
        )
        self.risk = RiskEngine(cfg.risk, self.costs, self.kill)
        self.router = Router(cfg.execution, self.costs)
        self.features = FeaturePipeline(cfg.data.coins)
        self.throttle = Throttle(strategy.min_interval_ms())
        self.fills = FillTracker()
        self.calibrator = SlippageCalibrator()

        self._books: dict[str, BookSnapshot] = {}
        self._ctx: dict[str, PerpContext] = {}
        self._positions: dict[str, Position] = {}
        self._open: dict[str, OpenTrade] = {}
        self._decision_mid: dict[str, float] = {}
        self._running = False
        self._ws: Optional[HyperliquidWS] = None

    # ---- startup ----------------------------------------------------------

    async def start(self) -> None:
        from ..execution.reconciler import Reconciler

        log.event("supervisor_starting", mode=self.cfg.mode.value,
                  coins=self.cfg.data.coins, strategy=self.strategy.name)

        restored = self.store.load_killswitch()
        if restored and restored.get("tripped"):
            # A bot that tripped on drawdown must not come back cheerful.
            self.kill.state.tripped = True
            log.warn("killswitch_restored_tripped", events=restored.get("events"))

        snapshot = await self.gateway.account()
        reconciler = Reconciler(self.store, self.kill)
        reconciler.set_local(self._positions)
        result = reconciler.reconcile(snapshot, startup=True)
        if not await reconciler.adopt_or_abort(result, self.gateway, startup=True):
            raise RuntimeError("startup reconciliation failed; refusing to trade")

        self._positions = result.positions
        for coin, pos in self._positions.items():
            self.fills.sync(coin, pos.size)
        self.risk.update_equity(result.equity_usd)
        self.kill.check_equity(result.equity_usd, int(time.time() * 1000))

        if self.cfg.mode is Mode.LIVE:
            starter = getattr(self.gateway, "start_dead_man_switch", None)
            if starter:
                await starter()

        self._reconciler = reconciler
        self._running = True

    # ---- main loop --------------------------------------------------------

    async def run(self) -> None:
        await self.start()
        subs = []
        for coin in self.cfg.data.coins:
            subs += [
                Subscription("l2Book", coin=coin),
                Subscription("trades", coin=coin),
                Subscription("activeAssetCtx", coin=coin),
            ]

        housekeeping = asyncio.create_task(self._housekeeping())
        try:
            async with HyperliquidWS(
                self.cfg.network.ws_url, subs,
                ping_interval_s=self.cfg.data.ws_ping_interval_s,
                max_backoff_s=self.cfg.data.ws_reconnect_max_backoff_s,
                staleness_budget_ms=self.cfg.data.max_staleness_ms,
            ) as ws:
                self._ws = ws
                async for msg in ws:
                    await self._handle(msg)
        finally:
            housekeeping.cancel()
            await self.shutdown()

    async def _handle(self, msg) -> None:
        data = msg.raw.get("data")
        if data is None:
            return
        try:
            if msg.channel == "l2Book":
                book = parse_book(data, msg.local_ms)
                self._books[book.coin] = book
                self.features.on_book(book, msg.after_gap)
                on_book = getattr(self.gateway, "on_book", None)
                if on_book:
                    on_book(book)
                await self._evaluate(book)
            elif msg.channel == "trades":
                for tr in parse_trades(data, msg.local_ms):
                    self.features.on_trade(tr, msg.after_gap)
                    on_trade = getattr(self.gateway, "on_trade", None)
                    if on_trade:
                        for f in on_trade(tr, msg.local_ms) or []:
                            self._on_fill(f)
            elif msg.channel == "activeAssetCtx":
                ctx = parse_perp_ctx(data, msg.local_ms)
                self._ctx[ctx.coin] = ctx
                self.features.on_ctx(ctx, msg.after_gap)
        except Exception:
            log.exception("event_handler_failed", channel=msg.channel)

    async def _evaluate(self, book: BookSnapshot) -> None:
        now = int(time.time() * 1000)

        # 1. Are we allowed to believe our data?
        if self._ws is not None:
            age = self._ws.monitor.worst_age_ms(now)
            if self.kill.check_staleness(age, now):
                return

        await self._manage_open(book, now)

        # 2. Are we allowed to trade?
        if self.kill.tripped:
            return
        if not self.throttle.ready(book.coin, now):
            return

        # 3. What do we know?
        fv = self.features.snapshot(book.coin, now)
        if fv is None or not fv.complete:
            return

        # 4. What does the strategy want?
        intent = self.strategy.on_features(
            features=fv, book=book, position=self._positions.get(book.coin),
            ctx=self._ctx.get(book.coin), now_ms=now,
        )
        if intent is None:
            return

        # 5. How much, if any?
        inst = self.registry.get(intent.coin)
        ctx = self._ctx.get(intent.coin)
        decision = self.risk.evaluate(
            intent, inst, book, self._positions,
            funding_rate_hourly=ctx.funding if ctx else 0.0,
            expected_hold_hours=self.cfg.model.horizon_ms / 3_600_000,
            maker_entry=self.cfg.execution.prefer_maker,
            marks=self._marks(),
        )
        if not decision.approved:
            self.shadow.record(
                ts_ms=now, coin=intent.coin, side=intent.side.value,
                confidence=intent.confidence, expected_edge_bps=intent.expected_edge_bps,
                rejected_by="risk", reason=decision.reason, mid=book.mid or 0.0,
                features=fv.values, diagnostics=decision.diagnostics,
            )
            log.debug("intent_rejected", coin=intent.coin, reason=decision.reason,
                      **decision.diagnostics)
            return

        # 6 & 7. Execute and journal.
        await self._execute(intent, decision, inst, book, fv.values, now)

    async def _execute(self, intent, decision, inst, book, features, now_ms) -> None:
        self._decision_mid[intent.coin] = book.mid or 0.0

        def on_order(order, snap) -> None:
            self.store.record_order(OrderRecord(
                cloid=order.cloid, coin=order.coin, side=order.side.value,
                sz=order.sz, limit_px=order.limit_px, intent_reason=intent.reason,
                stop_px=intent.stop_px, take_profit_px=intent.take_profit_px,
                confidence=intent.confidence, expected_edge_bps=intent.expected_edge_bps,
                cost_bps=decision.cost.total_bps if decision.cost else 0.0,
                decision_mid=snap.mid or 0.0, features=dict(features),
                created_ms=order.created_ms,
            ))

        report = await self.router.execute(
            gateway=self.gateway, instrument=inst, side=intent.side,
            size=decision.size, book=book, on_order_created=on_order,
        )
        for f in report.fills:
            self._on_fill(f)
        self.kill.record_order_outcome(bool(report.aborted_reason), now_ms)

        if report.filled_size <= 0:
            log.warn("execution_no_fill", coin=intent.coin, reason=report.aborted_reason)
            return

        # Protective levels are built from the FILLED size, never the requested.
        self._open[intent.coin] = OpenTrade(
            coin=intent.coin, side=intent.side, entry_px=report.avg_px,
            size=report.filled_size, entry_ms=now_ms,
            stop_px=intent.stop_px, target_px=intent.take_profit_px,
            reason=intent.reason, confidence=intent.confidence,
            expected_edge_bps=intent.expected_edge_bps, features=dict(features),
        )
        log.event("position_opened", coin=intent.coin, side=intent.side.value,
                  size=report.filled_size, requested=report.requested_size,
                  avg_px=report.avg_px, partial=report.is_partial,
                  requotes=report.requotes, reason=intent.reason)

    def _marks(self) -> dict[str, float]:
        """Latest mid per coin, so portfolio-level notional is computed with
        each position valued at its own price."""
        return {c: b.mid for c, b in self._books.items() if b.mid is not None}

    def _on_fill(self, fill: Fill) -> None:
        self.fills.on_fill(fill)
        mid = self._decision_mid.get(fill.coin)
        if mid:
            self.calibrator.observe(mid, fill)
        pos = self._positions.get(fill.coin, Position(coin=fill.coin))
        new_size = pos.size + fill.sz * fill.side.sign
        self._positions[fill.coin] = Position(
            coin=fill.coin,
            size=new_size if abs(new_size) > 1e-12 else 0.0,
            entry_px=fill.px if abs(pos.size) < 1e-12 else pos.entry_px,
        )
        if fill.cloid:
            self.store.resolve_order(fill.cloid)

    async def _manage_open(self, book: BookSnapshot, now_ms: int) -> None:
        trade = self._open.get(book.coin)
        pos = self._positions.get(book.coin)
        if trade is None or pos is None or pos.is_flat:
            return
        mid = book.mid
        if mid is None:
            return

        reason = None
        if trade.stop_px is not None:
            if (trade.side is Side.BUY and mid <= trade.stop_px) or \
               (trade.side is Side.SELL and mid >= trade.stop_px):
                reason = "stop"
        if reason is None and trade.target_px is not None:
            if (trade.side is Side.BUY and mid >= trade.target_px) or \
               (trade.side is Side.SELL and mid <= trade.target_px):
                reason = "target"
        if reason is None and now_ms - trade.entry_ms > self.cfg.model.horizon_ms:
            reason = "time"
        if reason is None and self.kill.must_flatten:
            reason = "killswitch"
        if reason:
            await self._close(book.coin, book, now_ms, reason)

    async def _close(self, coin: str, book: BookSnapshot, now_ms: int, reason: str) -> None:
        trade = self._open.pop(coin, None)
        pos = self._positions.get(coin)
        if trade is None or pos is None or pos.is_flat:
            return
        inst = self.registry.get(coin)
        report = await self.router.execute(
            gateway=self.gateway, instrument=inst, side=trade.side.opposite,
            size=abs(pos.size), book=book, reduce_only=True,
        )
        for f in report.fills:
            self._on_fill(f)
        if report.filled_size <= 0:
            log.error("close_failed", coin=coin, reason=reason, error=report.aborted_reason)
            # Put it back: an un-closed position is still ours to manage.
            self._open[coin] = trade
            return

        exit_px = report.avg_px
        gross = (exit_px - trade.entry_px) * report.filled_size * trade.side.sign
        realised_bps = 1e4 * (exit_px - trade.entry_px) / trade.entry_px * trade.side.sign
        from .state import TradeAttribution

        self.store.append_trade(TradeAttribution(
            coin=coin, entry_ms=trade.entry_ms, exit_ms=now_ms, side=trade.side.value,
            entry_px=trade.entry_px, exit_px=exit_px, size=report.filled_size,
            gross_pnl=gross, fees=report.fees, funding=0.0,
            entry_reason=trade.reason, exit_reason=reason,
            confidence=trade.confidence, expected_edge_bps=trade.expected_edge_bps,
            realised_bps=realised_bps, features=trade.features,
        ))
        net = gross - report.fees
        self.kill.record_trade_result(net, now_ms)
        log.event("position_closed", coin=coin, reason=reason, net_pnl=round(net, 4),
                  realised_bps=round(realised_bps, 2),
                  expected_bps=round(trade.expected_edge_bps, 2))

    # ---- periodic ---------------------------------------------------------

    async def _housekeeping(self) -> None:
        while self._running:
            await asyncio.sleep(self.cfg.execution.reconcile_interval_s)
            try:
                snapshot = await self.gateway.account()
                self._reconciler.set_local(self._positions)
                result = self._reconciler.reconcile(snapshot, startup=False)
                await self._reconciler.adopt_or_abort(result, self.gateway, startup=False)
                self._positions = result.positions
                for coin, pos in result.positions.items():
                    self.fills.sync(coin, pos.size)

                self.risk.update_equity(result.equity_usd)
                self.kill.check_equity(result.equity_usd, int(time.time() * 1000))
                self.store.save_killswitch(self.kill.status())

                suggested = self.calibrator.suggested_residual_bps()
                if suggested is not None and abs(suggested - self.costs.residual_slippage_bps) > 0.5:
                    log.warn("residual_slippage_drift",
                             configured=self.costs.residual_slippage_bps,
                             measured=suggested, **self.calibrator.summary())

                if self.kill.must_flatten:
                    await self._flatten_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping_failed")

    async def _flatten_all(self) -> None:
        log.warn("flattening_all_positions", reason="killswitch")
        await self.gateway.cancel_all()
        for coin in list(self._open):
            book = self._books.get(coin)
            if book:
                await self._close(coin, book, int(time.time() * 1000), "killswitch")

    async def shutdown(self) -> None:
        self._running = False
        log.event("supervisor_stopping", positions=len(self._positions))
        try:
            await self.gateway.cancel_all()
        except Exception:
            log.exception("shutdown_cancel_failed")
        self.store.save_killswitch(self.kill.status())
        await self.gateway.close()
