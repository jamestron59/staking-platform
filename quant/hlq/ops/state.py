"""Crash-safe local state.

The question this answers: the process dies holding a position — what happens
when it starts again?

The wrong answer, and the common one, is "it starts flat and opens a new
position", which doubles exposure. The right answer is that local state is a
*cache*, the exchange is the truth, and startup reconciles the two before any
strategy code runs.

So this file persists only what the exchange cannot tell us:
  - which cloids we minted and their intent, so an orphan order found on the
    exchange can be attributed rather than blindly cancelled;
  - kill-switch state, so a bot that tripped on drawdown does not come back
    cheerful after a restart;
  - trade attribution (entry reason, features at decision time) for the model
    layer.

Writes are atomic: write to a temp file in the same directory, fsync, then
`os.replace`. A crash mid-write leaves the previous good version, never a
half-written one.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Optional

from ..logging_setup import get_logger

log = get_logger(__name__)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, default=_encode, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # fsync the directory too, or the rename itself can be lost on power loss.
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _encode(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


@dataclass
class OrderRecord:
    """Our side of an order's story. The exchange knows the order; only we know
    why we sent it."""

    cloid: str
    coin: str
    side: str
    sz: float
    limit_px: float
    intent_reason: str
    stop_px: Optional[float] = None
    take_profit_px: Optional[float] = None
    confidence: float = 0.0
    expected_edge_bps: float = 0.0
    cost_bps: float = 0.0
    decision_mid: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    created_ms: int = 0
    resolved: bool = False


@dataclass
class TradeAttribution:
    """One closed round trip with everything needed to learn from it.

    `features` is the point-in-time feature vector at entry. Recording it here
    rather than recomputing later is what makes the training set honest: a
    feature recomputed from history can accidentally use data that arrived
    after the decision.
    """

    coin: str
    entry_ms: int
    exit_ms: int
    side: str
    entry_px: float
    exit_px: float
    size: float
    gross_pnl: float
    fees: float
    funding: float
    entry_reason: str
    exit_reason: str
    confidence: float
    expected_edge_bps: float
    realised_bps: float
    features: dict[str, float] = field(default_factory=dict)


class StateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._orders_path = self.root / "orders.json"
        self._kill_path = self.root / "killswitch.json"
        self._journal_path = self.root / "trades.jsonl"
        self.orders: dict[str, OrderRecord] = {}
        self._load()

    def _load(self) -> None:
        if self._orders_path.exists():
            try:
                raw = json.loads(self._orders_path.read_text())
                self.orders = {k: OrderRecord(**v) for k, v in raw.items()}
                log.event("state_loaded", open_orders=len(self.orders))
            except Exception as exc:
                # A corrupt state file must not silently become an empty one:
                # that would look like "no open orders" to the reconciler.
                log.error("state_load_failed", error=str(exc), path=str(self._orders_path))
                raise

    def record_order(self, rec: OrderRecord) -> None:
        self.orders[rec.cloid] = rec
        self._flush_orders()

    def resolve_order(self, cloid: str) -> None:
        rec = self.orders.get(cloid)
        if rec:
            rec.resolved = True
            self._flush_orders()

    def prune_resolved(self, older_than_ms: int) -> int:
        cutoff = int(time.time() * 1000) - older_than_ms
        before = len(self.orders)
        self.orders = {
            k: v for k, v in self.orders.items() if not (v.resolved and v.created_ms < cutoff)
        }
        if len(self.orders) != before:
            self._flush_orders()
        return before - len(self.orders)

    def unresolved(self) -> list[OrderRecord]:
        return [r for r in self.orders.values() if not r.resolved]

    def _flush_orders(self) -> None:
        atomic_write_json(self._orders_path, {k: asdict(v) for k, v in self.orders.items()})

    def save_killswitch(self, payload: dict) -> None:
        atomic_write_json(self._kill_path, payload)

    def load_killswitch(self) -> Optional[dict]:
        if not self._kill_path.exists():
            return None
        try:
            return json.loads(self._kill_path.read_text())
        except Exception as exc:
            log.error("killswitch_state_load_failed", error=str(exc))
            return None

    def append_trade(self, attribution: TradeAttribution) -> None:
        """Append-only journal. This is the training set for the model layer and
        the audit trail for every post-mortem."""
        with open(self._journal_path, "a") as fh:
            fh.write(json.dumps(asdict(attribution), default=_encode, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_trades(self) -> list[TradeAttribution]:
        if not self._journal_path.exists():
            return []
        out = []
        for line in self._journal_path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(TradeAttribution(**json.loads(line)))
                except Exception:
                    continue
        return out


class ShadowJournal:
    """Signals we generated but did NOT trade.

    Without this the auto-learning loop trains only on executed trades, which
    is a censored sample: every filter you apply becomes invisible to the model
    that is supposed to evaluate your filters. Recording rejected signals with
    the reason and the market state is what makes it possible to ask later
    "was that filter actually helping?" — the counterfactual that the original
    architecture's retraining step had no way to see.
    """

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "shadow_signals.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        ts_ms: int,
        coin: str,
        side: str,
        confidence: float,
        expected_edge_bps: float,
        rejected_by: str,
        reason: str,
        mid: float,
        features: dict[str, float],
        diagnostics: dict | None = None,
    ) -> None:
        payload = {
            "ts": ts_ms, "coin": coin, "side": side, "confidence": confidence,
            "edge_bps": expected_edge_bps, "rejected_by": rejected_by, "reason": reason,
            "mid": mid, "features": features, "diagnostics": diagnostics or {},
        }
        with open(self.path, "a") as fh:
            fh.write(json.dumps(payload, default=_encode, separators=(",", ":")) + "\n")
