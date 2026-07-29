"""Model registry: champion / challenger with promotion gates.

The original design retrained every 1,000 trades and deployed the result. Three
things go wrong with that, and this module is the answer to each:

**Retraining on your own fills is a censored sample.** You only observe outcomes
for trades you took. Every filter you apply is invisible to the retraining step,
so it can never tell you a filter is hurting. Fixed by `ops.state.ShadowJournal`,
which records rejected signals too — and by `min_shadow_samples` here, which
refuses to promote a model that has not been evaluated on them.

**"Every 1,000 trades" is not a unit of time.** On a 1-minute horizon that can
be a fortnight — one regime. You fit the regime, deploy, and the regime ends.
Promotion here is gated on elapsed shadow time, not trade count.

**Automatic promotion to live capital.** Never. A challenger runs in shadow
mode, producing signals that are scored but not traded, and must beat the
incumbent out-of-sample by a margin before it gets capital. Then it starts at
a fraction of full size.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..logging_setup import get_logger

log = get_logger(__name__)


class ModelStage(str, Enum):
    CHALLENGER = "challenger"  # training complete, no capital
    SHADOW = "shadow"  # producing scored signals, still no capital
    CHAMPION = "champion"  # live, ramping or at full size
    RETIRED = "retired"


@dataclass
class ModelMetrics:
    oos_sharpe: float = 0.0
    deflated_sharpe: float = 0.0
    brier: float = 1.0
    brier_skill_score: float = 0.0
    expected_calibration_error: float = 1.0
    n_train: int = 0
    n_oos: int = 0
    n_shadow: int = 0
    net_bps_per_trade: float = 0.0
    n_trials: int = 1


@dataclass
class ModelRecord:
    model_id: str
    created_ms: int
    stage: ModelStage
    metrics: ModelMetrics = field(default_factory=ModelMetrics)
    feature_names: list[str] = field(default_factory=list)
    shadow_started_ms: int = 0
    promoted_ms: int = 0
    capital_fraction: float = 0.0
    notes: str = ""

    @property
    def shadow_hours(self) -> float:
        if not self.shadow_started_ms:
            return 0.0
        return (time.time() * 1000 - self.shadow_started_ms) / 3_600_000


@dataclass
class PromotionGate:
    min_oos_sharpe: float
    max_brier: float
    min_improvement: float
    shadow_hours: int
    min_shadow_samples: int = 200
    min_deflated_sharpe: float = 0.95
    max_calibration_error: float = 0.10

    def evaluate(
        self, challenger: ModelRecord, champion: Optional[ModelRecord]
    ) -> tuple[bool, list[str]]:
        """Returns (may_promote, blocking_reasons). Default answer is no."""
        m = challenger.metrics
        reasons: list[str] = []

        if challenger.stage is not ModelStage.SHADOW:
            reasons.append(f"stage is {challenger.stage.value}, must complete a shadow period first")
        if challenger.shadow_hours < self.shadow_hours:
            reasons.append(
                f"shadow period {challenger.shadow_hours:.1f}h < required {self.shadow_hours}h"
            )
        if m.n_shadow < self.min_shadow_samples:
            reasons.append(f"only {m.n_shadow} shadow signals < {self.min_shadow_samples}")
        if m.oos_sharpe < self.min_oos_sharpe:
            reasons.append(f"OOS Sharpe {m.oos_sharpe:.2f} < {self.min_oos_sharpe}")
        if m.deflated_sharpe < self.min_deflated_sharpe:
            reasons.append(
                f"deflated Sharpe {m.deflated_sharpe:.3f} < {self.min_deflated_sharpe} "
                f"across {m.n_trials} declared trials"
            )
        if m.brier > self.max_brier:
            reasons.append(f"Brier {m.brier:.3f} > {self.max_brier}")
        if m.brier_skill_score <= 0:
            reasons.append("Brier skill score <= 0: no better than predicting the base rate")
        if m.expected_calibration_error > self.max_calibration_error:
            reasons.append(
                f"calibration error {m.expected_calibration_error:.3f} > {self.max_calibration_error}"
            )
        if m.net_bps_per_trade <= 0:
            reasons.append("net bps per trade after costs is not positive")

        if champion is not None:
            required = champion.metrics.oos_sharpe * (1 + self.min_improvement)
            if m.oos_sharpe < required:
                reasons.append(
                    f"OOS Sharpe {m.oos_sharpe:.2f} does not beat champion "
                    f"{champion.metrics.oos_sharpe:.2f} by {100*self.min_improvement:.0f}%"
                )
        return (not reasons), reasons


class ModelRegistry:
    def __init__(self, root: str | Path, gate: PromotionGate) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.gate = gate
        self._path = self.root / "registry.json"
        self.records: dict[str, ModelRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        for mid, r in raw.items():
            r["stage"] = ModelStage(r["stage"])
            r["metrics"] = ModelMetrics(**r.get("metrics", {}))
            self.records[mid] = ModelRecord(**r)

    def _save(self) -> None:
        from ..ops.state import atomic_write_json

        atomic_write_json(self._path, {k: asdict(v) for k, v in self.records.items()})

    def register(self, record: ModelRecord) -> None:
        self.records[record.model_id] = record
        self._save()
        log.event("model_registered", model_id=record.model_id, stage=record.stage.value)

    def champion(self) -> Optional[ModelRecord]:
        return next((r for r in self.records.values() if r.stage is ModelStage.CHAMPION), None)

    def start_shadow(self, model_id: str) -> None:
        rec = self.records[model_id]
        rec.stage = ModelStage.SHADOW
        rec.shadow_started_ms = int(time.time() * 1000)
        self._save()
        log.event("model_shadow_started", model_id=model_id,
                  required_hours=self.gate.shadow_hours)

    def try_promote(self, model_id: str, *, initial_capital_fraction: float = 0.25) -> tuple[bool, list[str]]:
        """Promote only if every gate passes, and then only at partial size.

        Ramping matters: a model validated on history has never traded against
        a live book with real queue positions. Starting at a quarter size makes
        the first live sample cheap.
        """
        challenger = self.records[model_id]
        champ = self.champion()
        ok, reasons = self.gate.evaluate(challenger, champ)
        if not ok:
            log.warn("model_promotion_blocked", model_id=model_id, reasons=reasons)
            return False, reasons

        if champ is not None:
            champ.stage = ModelStage.RETIRED
            log.event("model_retired", model_id=champ.model_id)
        challenger.stage = ModelStage.CHAMPION
        challenger.promoted_ms = int(time.time() * 1000)
        challenger.capital_fraction = initial_capital_fraction
        self._save()
        log.event("model_promoted", model_id=model_id, capital_fraction=initial_capital_fraction)
        return True, []

    def ramp(self, model_id: str, fraction: float) -> None:
        rec = self.records[model_id]
        if rec.stage is not ModelStage.CHAMPION:
            raise ValueError(f"{model_id} is not the champion")
        rec.capital_fraction = min(1.0, max(0.0, fraction))
        self._save()
        log.event("model_ramped", model_id=model_id, capital_fraction=rec.capital_fraction)

    def rollback(self, reason: str) -> Optional[str]:
        """Retire the champion and restore the most recent retired model.

        The path that must exist before a model ever takes capital: if you
        cannot roll back in one command, you will hesitate when it matters.
        """
        champ = self.champion()
        if champ is None:
            return None
        champ.stage = ModelStage.RETIRED
        champ.notes = f"rolled back: {reason}"
        prior = sorted(
            (r for r in self.records.values()
             if r.stage is ModelStage.RETIRED and r.model_id != champ.model_id),
            key=lambda r: r.promoted_ms, reverse=True,
        )
        restored = None
        if prior:
            prior[0].stage = ModelStage.CHAMPION
            prior[0].capital_fraction = 0.25
            restored = prior[0].model_id
        self._save()
        log.warn("model_rolled_back", retired=champ.model_id, restored=restored, reason=reason)
        return restored

    def summary(self) -> dict:
        return {
            mid: {
                "stage": r.stage.value,
                "capital_fraction": r.capital_fraction,
                "oos_sharpe": r.metrics.oos_sharpe,
                "deflated_sharpe": r.metrics.deflated_sharpe,
                "brier": r.metrics.brier,
                "shadow_hours": round(r.shadow_hours, 1),
            }
            for mid, r in self.records.items()
        }
