"""Probability calibration.

A model that outputs 0.88 is not claiming 88% — it is claiming whatever its
loss function happened to produce. Gradient-boosted trees in particular are
badly calibrated by default, pushing scores toward the extremes.

This matters here more than in most applications because the whole risk
framework is stated in probabilities. `min_edge_after_costs_bps` compares
expected edge to cost, and expected edge is computed from p. If p is
systematically 15 points too high, every position is oversized and every gate
passes when it should not. The original design's "confidence 0-100, trade above
80" is precisely this failure: a threshold on an uncalibrated number.

Isotonic regression is used rather than Platt scaling because it makes no
assumption about the shape of the miscalibration, and we have no reason to
believe it is sigmoid. The pool-adjacent-violators implementation is
self-contained so the serving path has no sklearn dependency.

The number to watch is not accuracy but the **Brier score** and its reliability
component. A model can be 60% accurate and useless if its confident predictions
are no better than its unconfident ones.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence


def _pav(x: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators: the isotonic fit, in one pass.

    Produces a non-decreasing step function through (x, y) minimising squared
    error. Blocks are merged whenever monotonicity is violated.
    """
    order = sorted(range(len(x)), key=lambda i: x[i])
    xs = [x[i] for i in order]
    ys = [y[i] for i in order]

    values: list[float] = []
    weights: list[float] = []
    bounds: list[float] = []
    for xi, yi in zip(xs, ys):
        values.append(yi)
        weights.append(1.0)
        bounds.append(xi)
        while len(values) > 1 and values[-2] > values[-1]:
            w = weights[-1] + weights[-2]
            v = (values[-1] * weights[-1] + values[-2] * weights[-2]) / w
            values.pop()
            weights.pop()
            bounds.pop()
            values[-1], weights[-1] = v, w
    return bounds, values


@dataclass
class IsotonicCalibrator:
    """Maps raw model scores to calibrated probabilities."""

    thresholds: list[float] = field(default_factory=list)
    probabilities: list[float] = field(default_factory=list)
    n_samples: int = 0

    def fit(self, scores: Sequence[float], outcomes: Sequence[int]) -> "IsotonicCalibrator":
        if len(scores) != len(outcomes):
            raise ValueError("scores and outcomes must be the same length")
        if len(scores) < 50:
            raise ValueError(
                f"only {len(scores)} samples: calibrating on this little data "
                "produces a calibrator that itself needs calibrating"
            )
        self.thresholds, self.probabilities = _pav(scores, [float(o) for o in outcomes])
        self.n_samples = len(scores)
        return self

    def transform(self, score: float) -> float:
        if not self.thresholds:
            return score
        i = bisect.bisect_right(self.thresholds, score) - 1
        i = max(0, min(i, len(self.probabilities) - 1))
        return min(1.0, max(0.0, self.probabilities[i]))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({
            "thresholds": self.thresholds,
            "probabilities": self.probabilities,
            "n_samples": self.n_samples,
        }))

    @classmethod
    def load(cls, path: str | Path) -> "IsotonicCalibrator":
        d = json.loads(Path(path).read_text())
        return cls(d["thresholds"], d["probabilities"], d.get("n_samples", 0))


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of probabilistic predictions. Lower is better.

    Always predicting the base rate scores p(1-p) — about 0.25 for a balanced
    problem. A model above that is worse than a constant.
    """
    if not probabilities:
        return 1.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(probabilities)


@dataclass
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float

    @property
    def gap(self) -> float:
        return self.mean_predicted - self.observed_rate


def reliability_curve(
    probabilities: Sequence[float], outcomes: Sequence[int], n_bins: int = 10
) -> list[ReliabilityBin]:
    """Predicted vs observed, bucketed. The diagnostic to look at before any
    threshold is chosen: if the 0.8-0.9 bucket resolves at 0.55, an 80%
    confidence gate is admitting coin flips."""
    bins: list[ReliabilityBin] = []
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        idx = [i for i, p in enumerate(probabilities) if (lo <= p < hi or (b == n_bins - 1 and p == 1.0))]
        if not idx:
            continue
        bins.append(ReliabilityBin(
            lower=lo, upper=hi, count=len(idx),
            mean_predicted=sum(probabilities[i] for i in idx) / len(idx),
            observed_rate=sum(outcomes[i] for i in idx) / len(idx),
        ))
    return bins


def calibration_error(bins: Sequence[ReliabilityBin]) -> float:
    """Expected calibration error: average |predicted - observed|, weighted."""
    total = sum(b.count for b in bins)
    if total == 0:
        return 1.0
    return sum(b.count * abs(b.gap) for b in bins) / total


def calibration_report(
    probabilities: Sequence[float], outcomes: Sequence[int], n_bins: int = 10
) -> dict:
    bins = reliability_curve(probabilities, outcomes, n_bins)
    base_rate = sum(outcomes) / len(outcomes) if outcomes else 0.0
    bs = brier_score(probabilities, outcomes)
    baseline = base_rate * (1 - base_rate)
    return {
        "n": len(probabilities),
        "base_rate": round(base_rate, 4),
        "brier": round(bs, 4),
        "brier_of_always_predicting_base_rate": round(baseline, 4),
        # Below zero means the model is worse than a constant prediction.
        "brier_skill_score": round(1 - bs / baseline, 4) if baseline > 0 else 0.0,
        "expected_calibration_error": round(calibration_error(bins), 4),
        "bins": [
            {
                "range": f"{b.lower:.1f}-{b.upper:.1f}",
                "n": b.count,
                "predicted": round(b.mean_predicted, 3),
                "observed": round(b.observed_rate, 3),
                "gap": round(b.gap, 3),
            }
            for b in bins
        ],
    }


def expected_edge_bps(
    calibrated_p: float, target_bps: float, stop_bps: float
) -> float:
    """Turn a calibrated probability into the number risk actually needs.

    This is the bridge between the model and the risk engine, and the reason
    calibration is not optional: an 88% confidence on a 5bps target with a
    20bps stop has an expected value of -0.6bps. Confidence alone cannot see
    that; expected value can.
    """
    p = min(1.0, max(0.0, calibrated_p))
    return p * target_bps - (1 - p) * stop_bps
