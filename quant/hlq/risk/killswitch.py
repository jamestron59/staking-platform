"""Kill switches.

Ranked by how often each one actually saves money in practice, which is close
to the inverse of how much attention they usually get:

  1. **Data staleness.** The bot is blind and does not know it. Trading on a
     four-minute-old book is the single most expensive failure mode and the
     easiest to detect.
  2. **State divergence.** Local position does not match the exchange's. Every
     subsequent risk calculation is then wrong, including the ones that would
     otherwise stop the bleeding.
  3. **Reject rate.** A spike means something structural broke — bad rounding,
     rate limits, insufficient margin. Retrying into it burns capital and
     rate-limit budget.
  4. **Daily loss / drawdown / losing streak.** The ones everyone builds first,
     and the ones that trigger last, after the damage is already done.

Design rule: tripping is *sticky*. A switch that resets itself as soon as the
condition clears will re-enter a market that is still broken. Clearing requires
an explicit `reset()`, which in live operation means a human looked at it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..logging_setup import get_logger

log = get_logger(__name__)


class TripReason(str, Enum):
    DATA_STALE = "data_stale"
    STATE_DIVERGENCE = "state_divergence"
    REJECT_RATE = "reject_rate"
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"
    LOSS_STREAK = "loss_streak"
    LIQUIDATION_RISK = "liquidation_risk"
    MANUAL = "manual"


@dataclass
class TripEvent:
    reason: TripReason
    detail: str
    at_ms: int
    # Flat means: close everything now. Otherwise stop opening new risk but
    # manage what is open — closing into a broken feed can be worse than holding.
    require_flat: bool = False


@dataclass
class KillSwitchState:
    tripped: bool = False
    events: list[TripEvent] = field(default_factory=list)
    day_start_equity: float = 0.0
    day_key: str = ""
    peak_equity: float = 0.0
    consecutive_losses: int = 0


class KillSwitch:
    def __init__(
        self,
        *,
        max_daily_loss_pct: float,
        max_drawdown_pct: float,
        max_consecutive_losses: int,
        max_reject_rate: float,
        reject_window: int,
        max_staleness_ms: int,
    ) -> None:
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.max_reject_rate = max_reject_rate
        self.max_staleness_ms = max_staleness_ms
        self._rejects: deque[bool] = deque(maxlen=reject_window)
        self.state = KillSwitchState()

    # ---- lifecycle --------------------------------------------------------

    def _trip(self, reason: TripReason, detail: str, now_ms: int, require_flat: bool = False) -> TripEvent:
        ev = TripEvent(reason=reason, detail=detail, at_ms=now_ms, require_flat=require_flat)
        self.state.tripped = True
        self.state.events.append(ev)
        log.error("killswitch_tripped", reason=reason.value, detail=detail, require_flat=require_flat)
        return ev

    def trip_manual(
        self, reason: TripReason, detail: str, now_ms: int, require_flat: bool = False
    ) -> TripEvent:
        """Public entry point for callers outside this module that have detected
        a hard stop condition of their own (e.g. the reconciler finding orders
        it cannot attribute)."""
        return self._trip(reason, detail, now_ms, require_flat)

    @property
    def tripped(self) -> bool:
        return self.state.tripped

    @property
    def must_flatten(self) -> bool:
        return any(e.require_flat for e in self.state.events)

    def reset(self, operator_note: str = "") -> None:
        """Deliberately manual. If this is ever called automatically on a timer,
        the kill switch has been disabled in all but name."""
        log.warn("killswitch_reset", events=len(self.state.events), note=operator_note)
        self.state.tripped = False
        self.state.events.clear()
        self.state.consecutive_losses = 0
        self._rejects.clear()

    # ---- checks -----------------------------------------------------------

    def check_staleness(self, worst_age_ms: int, now_ms: int) -> Optional[TripEvent]:
        if worst_age_ms > self.max_staleness_ms:
            return self._trip(
                TripReason.DATA_STALE,
                f"feed silent for {worst_age_ms}ms (budget {self.max_staleness_ms}ms)",
                now_ms,
                # Do NOT force-flatten on stale data: sending market orders while
                # blind is how a feed hiccup turns into a realised loss. Stop
                # opening risk and let existing stops (resting on the exchange,
                # not in this process) do their job.
                require_flat=False,
            )
        return None

    def check_divergence(
        self, coin: str, local_size: float, exchange_size: float, now_ms: int, tolerance: float = 1e-6
    ) -> Optional[TripEvent]:
        if abs(local_size - exchange_size) > tolerance:
            return self._trip(
                TripReason.STATE_DIVERGENCE,
                f"{coin}: local={local_size} exchange={exchange_size}",
                now_ms,
                require_flat=False,
            )
        return None

    def record_order_outcome(self, rejected: bool, now_ms: int) -> Optional[TripEvent]:
        self._rejects.append(rejected)
        if len(self._rejects) < self._rejects.maxlen:
            return None
        rate = sum(self._rejects) / len(self._rejects)
        if rate > self.max_reject_rate:
            return self._trip(
                TripReason.REJECT_RATE,
                f"{100*rate:.0f}% of last {len(self._rejects)} orders rejected",
                now_ms,
            )
        return None

    def record_trade_result(self, net_pnl: float, now_ms: int) -> Optional[TripEvent]:
        if net_pnl < 0:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.max_consecutive_losses:
                return self._trip(
                    TripReason.LOSS_STREAK,
                    f"{self.state.consecutive_losses} consecutive losses",
                    now_ms,
                )
        else:
            self.state.consecutive_losses = 0
        return None

    def check_equity(self, equity: float, now_ms: int) -> Optional[TripEvent]:
        s = self.state
        day_key = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
        if s.day_key != day_key:
            s.day_key, s.day_start_equity = day_key, equity
        if s.peak_equity <= 0:
            s.peak_equity = equity
        s.peak_equity = max(s.peak_equity, equity)

        if s.day_start_equity > 0:
            daily = 100 * (s.day_start_equity - equity) / s.day_start_equity
            if daily >= self.max_daily_loss_pct:
                return self._trip(
                    TripReason.DAILY_LOSS,
                    f"down {daily:.2f}% today (limit {self.max_daily_loss_pct}%)",
                    now_ms, require_flat=True,
                )
        if s.peak_equity > 0:
            dd = 100 * (s.peak_equity - equity) / s.peak_equity
            if dd >= self.max_drawdown_pct:
                return self._trip(
                    TripReason.DRAWDOWN,
                    f"drawdown {dd:.2f}% from peak (limit {self.max_drawdown_pct}%)",
                    now_ms, require_flat=True,
                )
        return None

    def check_liquidation_proximity(
        self, coin: str, buffer_multiple: Optional[float], min_required: float, now_ms: int
    ) -> Optional[TripEvent]:
        if buffer_multiple is not None and buffer_multiple < min_required:
            return self._trip(
                TripReason.LIQUIDATION_RISK,
                f"{coin}: only {buffer_multiple:.2f}x stop-widths from liquidation",
                now_ms, require_flat=True,
            )
        return None

    def status(self) -> dict:
        return {
            "tripped": self.state.tripped,
            "must_flatten": self.must_flatten,
            "consecutive_losses": self.state.consecutive_losses,
            "peak_equity": round(self.state.peak_equity, 2),
            "day_start_equity": round(self.state.day_start_equity, 2),
            "events": [
                {"reason": e.reason.value, "detail": e.detail, "at_ms": e.at_ms}
                for e in self.state.events
            ],
        }
