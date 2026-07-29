"""State reconciliation: the exchange is right, we are a cache.

Runs at startup and then periodically. It exists because of a specific class of
loss that has nothing to do with strategy quality: the bot believes it is flat
while holding 0.4 BTC, or believes it has a stop resting when the stop was
cancelled by a dead man's switch twenty minutes ago. Every risk calculation
downstream of a wrong position is also wrong.

Four divergences, each with a different correct response:

  - **Phantom position** (exchange has one, we do not). Almost always a fill we
    missed. Adopt it, then trip the kill switch — we do not know its entry
    reason, so we cannot manage it, only close it.
  - **Missing position** (we think we have one, exchange does not). We were
    stopped out, liquidated, or it never opened. Adopt flat. Not fatal.
  - **Orphan order** (resting on the exchange, unknown to us). If the cloid is
    in our journal it is ours from before a restart — adopt it. If it is not,
    something else is trading this account: refuse to continue.
  - **Size mismatch**. Partial fill we did not process. Adopt the exchange's.

The asymmetry is deliberate: adopting exchange truth is always safe, acting on
a divergence is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..logging_setup import get_logger
from ..ops.state import StateStore
from ..risk.killswitch import KillSwitch, TripReason
from ..types import Order, OrderStatus, Position
from .gateway import AccountSnapshot

log = get_logger(__name__)


@dataclass
class Divergence:
    kind: str
    coin: str
    detail: str
    severity: str  # "info" | "warn" | "fatal"


@dataclass
class ReconcileResult:
    positions: dict[str, Position] = field(default_factory=dict)
    open_orders: list[Order] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    equity_usd: float = 0.0

    @property
    def clean(self) -> bool:
        return not self.divergences

    @property
    def fatal(self) -> bool:
        return any(d.severity == "fatal" for d in self.divergences)


class Reconciler:
    def __init__(
        self, store: StateStore, kill: KillSwitch, *,
        size_tolerance: float = 1e-8, exclusive_account: bool = True,
    ) -> None:
        self.store = store
        self.kill = kill
        self.size_tolerance = size_tolerance
        # When false, this bot shares the HL account with something else, so
        # anything it cannot attribute belongs to that other process and must
        # never be cancelled or adopted.
        self.exclusive_account = exclusive_account
        self._local_positions: dict[str, Position] = {}

    def set_local(self, positions: dict[str, Position]) -> None:
        self._local_positions = dict(positions)

    def reconcile(self, snapshot: AccountSnapshot, *, startup: bool = False) -> ReconcileResult:
        now_ms = snapshot.at_ms or int(time.time() * 1000)
        result = ReconcileResult(
            positions=dict(snapshot.positions),
            open_orders=list(snapshot.open_orders),
            equity_usd=snapshot.equity_usd,
        )

        exchange_coins = set(snapshot.positions)
        local_coins = {c for c, p in self._local_positions.items() if not p.is_flat}

        for coin in exchange_coins - local_coins:
            pos = snapshot.positions[coin]
            result.divergences.append(Divergence(
                kind="phantom_position", coin=coin,
                detail=f"exchange holds {pos.size} @ {pos.entry_px}, local state has none",
                severity="info" if startup else "fatal",
            ))
            if not startup:
                self.kill.check_divergence(coin, 0.0, pos.size, now_ms)

        for coin in local_coins - exchange_coins:
            local = self._local_positions[coin]
            result.divergences.append(Divergence(
                kind="missing_position", coin=coin,
                detail=f"local believes {local.size}, exchange is flat (stopped out or liquidated?)",
                severity="warn",
            ))
            if not startup:
                self.kill.check_divergence(coin, local.size, 0.0, now_ms)

        for coin in exchange_coins & local_coins:
            ex, loc = snapshot.positions[coin].size, self._local_positions[coin].size
            if abs(ex - loc) > self.size_tolerance:
                result.divergences.append(Divergence(
                    kind="size_mismatch", coin=coin,
                    detail=f"exchange {ex} vs local {loc} (unprocessed partial fill?)",
                    severity="warn",
                ))
                self.kill.check_divergence(coin, loc, ex, now_ms)

        result.divergences.extend(self._check_orders(snapshot, startup))

        # Exchange truth is adopted unconditionally.
        self._local_positions = dict(snapshot.positions)

        if result.divergences:
            log.warn("reconcile_divergences", count=len(result.divergences), startup=startup,
                     items=[{"kind": d.kind, "coin": d.coin, "detail": d.detail} for d in result.divergences])
        else:
            log.debug("reconcile_clean", positions=len(result.positions), orders=len(result.open_orders))
        return result

    def _check_orders(self, snapshot: AccountSnapshot, startup: bool) -> list[Divergence]:
        out: list[Divergence] = []
        known = set(self.store.orders)
        for o in snapshot.open_orders:
            if o.cloid and o.cloid in known:
                continue
            if not o.cloid:
                out.append(Divergence(
                    kind="orphan_order", coin=o.coin,
                    detail=f"resting oid={o.oid} has no cloid: not placed by this bot",
                    severity="fatal",
                ))
            else:
                out.append(Divergence(
                    kind="unknown_cloid", coin=o.coin,
                    detail=f"resting cloid={o.cloid} is not in our journal",
                    severity="fatal",
                ))

        # The mirror case: we think an order rests, the exchange disagrees.
        resting = {o.cloid for o in snapshot.open_orders if o.cloid}
        for rec in self.store.unresolved():
            if rec.cloid not in resting:
                out.append(Divergence(
                    kind="stale_local_order", coin=rec.coin,
                    detail=f"cloid={rec.cloid} unresolved locally but not resting on exchange",
                    severity="info",
                ))
        return out

    async def adopt_or_abort(self, result: ReconcileResult, gateway, *, startup: bool) -> bool:
        """Returns True if it is safe to continue trading.

        On startup, an orphan order is recoverable: cancel everything, start
        from a known-clean slate. Mid-session it is not — something else is
        writing to this account and the only safe action is to stop.
        """
        if not result.fatal:
            return True

        if startup and not self.exclusive_account:
            # Shared account: unattributable orders are the other process's.
            # Cancelling them would be this bot reaching into another system's
            # state, and adopting them would mean managing positions whose
            # entry reason we do not know. Neither is acceptable, so stop.
            log.error(
                "startup_halt_shared_account",
                reason=(
                    "found orders on this account that this bot did not place. "
                    "exclusive_account is false, so they belong to another process "
                    "and will not be touched. Refusing to trade alongside it: give "
                    "this bot its own funded wallet, or stop the other process."
                ),
                orders=[d.detail for d in result.divergences if d.severity == "fatal"],
            )
            return False

        if startup:
            log.warn("startup_orphans_cancelling", count=len(result.divergences))
            await gateway.cancel_all()
            return True
        log.error("fatal_divergence_halting",
                  items=[d.detail for d in result.divergences if d.severity == "fatal"])
        self.kill.trip_manual(
            TripReason.STATE_DIVERGENCE,
            "unrecognised orders on the account; another process may be trading it",
            int(time.time() * 1000),
        )
        return False
