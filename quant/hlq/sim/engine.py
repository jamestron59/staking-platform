"""Event-driven backtester.

Replays recorded events in arrival order through the *production* feature
pipeline, the *production* risk engine, and a matching engine that models queue
position and latency. The strategy code is the same object the live loop uses.

What it deliberately does not do:
  - resample to candles (throws away the microstructure the features need);
  - fill at the mid;
  - assume maker orders fill;
  - let the strategy see an event before its arrival timestamp.

Position management (stops, targets, time exits) lives here rather than in the
strategy so that backtest and live share one implementation — a stop that
behaves differently in research than in production is a silent divergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..clock import LatencyModel, SimClock
from ..config import Config
from ..costs import CostModel
from ..data.replay import Event
from ..features.pipeline import FeaturePipeline
from ..instruments import InstrumentRegistry
from ..logging_setup import get_logger
from ..risk.engine import RiskEngine
from ..risk.killswitch import KillSwitch
from ..strategy.base import Strategy, Throttle
from ..types import BookSnapshot, Fill, Order, OrderStatus, PerpContext, Position, Side, TimeInForce, Trade
from .matching import MatchingEngine
from .metrics import BacktestResult, TradeRecord

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
    fees: float = 0.0
    funding: float = 0.0
    features: dict = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        cfg: Config,
        strategy: Strategy,
        registry: InstrumentRegistry,
        *,
        n_trials: int = 1,
        latency: Optional[LatencyModel] = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.registry = registry
        self.clock = SimClock()
        self.latency = latency or LatencyModel()
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
        self.features = FeaturePipeline(cfg.data.coins)
        self.matching = MatchingEngine(
            taker_fee_bps=cfg.costs.taker_fee_bps,
            maker_fee_bps=cfg.costs.maker_fee_bps,
            latency_ms=self.latency.total_ms,
        )
        self.throttle = Throttle(strategy.min_interval_ms())

        self.equity = cfg.risk.equity_usd
        self.result = BacktestResult(initial_equity=self.equity, n_trials=n_trials)
        self._books: dict[str, BookSnapshot] = {}
        self._ctx: dict[str, PerpContext] = {}
        self._positions: dict[str, Position] = {}
        self._open: dict[str, OpenTrade] = {}
        self._last_equity_ms = 0

    # ---- main loop --------------------------------------------------------

    def run(self, events: Iterable[Event]) -> BacktestResult:
        for ev in events:
            self.clock.advance(ev.local_ms)
            self.features.dispatch(ev)
            payload = ev.payload

            if isinstance(payload, BookSnapshot):
                self._books[payload.coin] = payload
                self._on_book(payload)
            elif isinstance(payload, Trade):
                self._on_trade(payload)
            elif isinstance(payload, PerpContext):
                self._ctx[payload.coin] = payload

            self._sample_equity(ev.local_ms)
        self._close_all("end_of_data")
        return self.result

    def _on_book(self, book: BookSnapshot) -> None:
        now = self.clock.now_ms()
        self._check_exits(book, now)
        if self.kill.tripped:
            return
        if not self.throttle.ready(book.coin, now):
            return

        fv = self.features.snapshot(book.coin, now)
        if fv is None or not fv.complete:
            return

        intent = self.strategy.on_features(
            features=fv, book=book,
            position=self._positions.get(book.coin),
            ctx=self._ctx.get(book.coin), now_ms=now,
        )
        if intent is None:
            return

        inst = self.registry.get(intent.coin)
        ctx = self._ctx.get(intent.coin)
        decision = self.risk.evaluate(
            intent, inst, book, self._positions,
            funding_rate_hourly=ctx.funding if ctx else 0.0,
            expected_hold_hours=self.cfg.model.horizon_ms / 3_600_000,
            maker_entry=self.cfg.execution.prefer_maker,
            marks={c: b.mid for c, b in self._books.items() if b.mid is not None},
        )
        if not decision.approved:
            self.result.rejections[decision.reason] = self.result.rejections.get(decision.reason, 0) + 1
            return

        self._enter(intent, decision.size, book, fv.values, now)

    def _on_trade(self, trade: Trade) -> None:
        now = self.clock.now_ms()
        for fill in self.matching.on_trade(trade, now):
            self._apply_fill(fill)
        book = self._books.get(trade.coin)
        if book:
            self._check_exits(book, now)

    # ---- position lifecycle ----------------------------------------------

    def _enter(
        self, intent, size: float, book: BookSnapshot, features: dict, now_ms: int
    ) -> None:
        inst = self.registry.get(intent.coin)
        from ..instruments import Rounding

        maker = self.cfg.execution.prefer_maker
        if maker:
            ref = book.best_bid if intent.side is Side.BUY else book.best_ask
            if ref is None:
                return
            px = inst.round_price(ref, Rounding.passive_for(intent.side))
            tif = TimeInForce.ALO
        else:
            mid = book.mid or 0.0
            px = inst.round_price(
                mid * (1 + intent.side.sign * self.cfg.execution.ioc_slippage_bps / 1e4),
                Rounding.aggressive_for(intent.side),
            )
            tif = TimeInForce.IOC

        order = Order(
            cloid=f"bt-{now_ms}-{intent.coin}", coin=intent.coin, side=intent.side,
            sz=size, limit_px=px, tif=tif, created_ms=now_ms,
        )
        fill, rejection = self.matching.submit(order, book, now_ms)
        if rejection:
            self.result.rejections[rejection] = self.result.rejections.get(rejection, 0) + 1
            return

        self._open[intent.coin] = OpenTrade(
            coin=intent.coin, side=intent.side, entry_px=px, size=size, entry_ms=now_ms,
            stop_px=intent.stop_px, target_px=intent.take_profit_px,
            reason=intent.reason, features=dict(features),
        )
        if fill:
            self._apply_fill(fill)

    def _apply_fill(self, fill: Fill) -> None:
        pos = self._positions.get(fill.coin, Position(coin=fill.coin))
        signed = fill.sz * fill.side.sign
        new_size = pos.size + signed
        open_trade = self._open.get(fill.coin)

        if open_trade and abs(new_size) > abs(pos.size):
            # Opening or adding: size the protective levels from what actually
            # filled, never from what was requested.
            open_trade.size = abs(new_size)
            open_trade.fees += fill.fee
            entry = (pos.entry_px * abs(pos.size) + fill.px * fill.sz) / abs(new_size) \
                if abs(pos.size) > 1e-12 else fill.px
            open_trade.entry_px = entry
        elif open_trade:
            open_trade.fees += fill.fee
            entry = pos.entry_px
        else:
            entry = fill.px

        self._positions[fill.coin] = Position(
            coin=fill.coin,
            size=new_size if abs(new_size) > 1e-12 else 0.0,
            entry_px=entry if abs(new_size) > 1e-12 else 0.0,
        )

    def _check_exits(self, book: BookSnapshot, now_ms: int) -> None:
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
            self._close(book.coin, book, now_ms, reason)

    def _close(self, coin: str, book: BookSnapshot, now_ms: int, reason: str) -> None:
        trade = self._open.pop(coin, None)
        pos = self._positions.get(coin)
        if trade is None or pos is None or pos.is_flat:
            return
        inst = self.registry.get(coin)
        from ..instruments import Rounding

        exit_side = trade.side.opposite
        mid = book.mid or trade.entry_px
        px = inst.round_price(
            mid * (1 + exit_side.sign * self.cfg.execution.ioc_slippage_bps / 1e4),
            Rounding.aggressive_for(exit_side),
        )
        order = Order(
            cloid=f"bt-exit-{now_ms}-{coin}", coin=coin, side=exit_side,
            sz=abs(pos.size), limit_px=px, tif=TimeInForce.IOC,
            reduce_only=True, created_ms=now_ms,
        )
        fill, _ = self.matching.submit(order, book, now_ms)
        exit_px = fill.px if fill else px
        exit_fee = fill.fee if fill else 0.0

        gross = (exit_px - trade.entry_px) * trade.size * trade.side.sign
        funding = self._accrued_funding(coin, trade, now_ms)
        rec = TradeRecord(
            coin=coin, entry_ms=trade.entry_ms, exit_ms=now_ms, side=trade.side.value,
            entry_px=trade.entry_px, exit_px=exit_px, size=trade.size,
            gross_pnl=gross, fees=trade.fees + exit_fee, funding=funding, reason=reason,
        )
        self.result.trades.append(rec)
        self.equity += rec.net_pnl
        self.kill.record_trade_result(rec.net_pnl, now_ms)
        self.kill.check_equity(self.equity, now_ms)
        self.risk.update_equity(self.equity)
        self._positions[coin] = Position(coin=coin)

    def _accrued_funding(self, coin: str, trade: OpenTrade, now_ms: int) -> float:
        ctx = self._ctx.get(coin)
        if ctx is None:
            return 0.0
        hours = (now_ms - trade.entry_ms) / 3_600_000
        return ctx.funding * hours * trade.size * trade.entry_px * trade.side.sign

    def _close_all(self, reason: str) -> None:
        for coin in list(self._open):
            book = self._books.get(coin)
            if book:
                self._close(coin, book, self.clock.now_ms(), reason)

    def _sample_equity(self, now_ms: int) -> None:
        """Sample hourly. Sampling per-event would make the return series
        dominated by book updates and inflate Sharpe by an order of magnitude."""
        if now_ms - self._last_equity_ms < 3_600_000:
            return
        self._last_equity_ms = now_ms
        unrealised = 0.0
        for coin, pos in self._positions.items():
            book = self._books.get(coin)
            if book and not pos.is_flat and book.mid:
                unrealised += pos.size * (book.mid - pos.entry_px)
        self.result.equity_curve.append(self.equity + unrealised)
        self.result.timestamps.append(now_ms)
