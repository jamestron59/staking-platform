"""Labelling, validation and calibration.

These tests encode the statistical claims the system rests on. If purging does
not purge, every out-of-sample number is contaminated and nothing else in the
repository is trustworthy.
"""

from __future__ import annotations

import math
import random

import pytest

from hlq.model.calibration import (
    IsotonicCalibrator, brier_score, calibration_report, expected_edge_bps, reliability_curve,
)
from hlq.model.labels import Label, PricePoint, label_distribution, meta_label, triple_barrier
from hlq.model.registry import ModelMetrics, ModelRecord, ModelRegistry, ModelStage, PromotionGate
from hlq.model.validation import PurgedKFold, split_report, walk_forward
from hlq.sim.metrics import BacktestResult, deflated_sharpe, sharpe, max_drawdown
from hlq.sim.metrics import TradeRecord


# ---- labels ---------------------------------------------------------------


def _series(moves: list[float], start: float = 100.0, step_ms: int = 1000) -> list[PricePoint]:
    px, ts, out = start, 0, []
    for m in moves:
        out.append(PricePoint(ts, px))
        px *= (1 + m)
        ts += step_ms
    out.append(PricePoint(ts, px))
    return out


def test_upper_barrier_first_is_a_win():
    prices = _series([0.0, 0.01, 0.01, 0.01])
    s = triple_barrier(prices, start_index=0, side=1, volatility=0.01,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=10_000,
                       round_trip_cost_bps=10.0)
    assert s is not None and s.label is Label.WIN and s.barrier_hit == "upper"
    assert s.t1_ms > s.t0_ms


def test_lower_barrier_first_is_a_loss():
    prices = _series([0.0, -0.01, -0.01, -0.01])
    s = triple_barrier(prices, start_index=0, side=1, volatility=0.01,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=10_000,
                       round_trip_cost_bps=10.0)
    assert s is not None and s.label is Label.LOSS


def test_flat_market_resolves_at_the_vertical_barrier():
    prices = _series([0.0] * 6)
    s = triple_barrier(prices, start_index=0, side=1, volatility=0.01,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=3_000,
                       round_trip_cost_bps=10.0)
    assert s is not None and s.barrier_hit == "vertical" and s.label is Label.NEUTRAL


def test_target_below_cost_is_dropped_not_labelled():
    """Training on trades whose target cannot clear costs teaches the model to
    find them. The sample is discarded instead."""
    prices = _series([0.0, 0.05])
    s = triple_barrier(prices, start_index=0, side=1, volatility=0.0001,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=10_000,
                       round_trip_cost_bps=20.0)
    assert s is None


def test_short_side_labels_invert():
    prices = _series([0.0, -0.02, -0.02])
    s = triple_barrier(prices, start_index=0, side=-1, volatility=0.01,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=10_000,
                       round_trip_cost_bps=10.0)
    assert s is not None and s.label is Label.WIN and s.ret > 0


def test_net_return_subtracts_costs():
    prices = _series([0.0, 0.02, 0.02])
    s = triple_barrier(prices, start_index=0, side=1, volatility=0.01,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=10_000,
                       round_trip_cost_bps=25.0)
    assert s is not None
    assert s.net_ret_bps == pytest.approx(1e4 * s.ret - 25.0)


def test_meta_label_is_profit_after_costs():
    prices = _series([0.0, 0.02, 0.02])
    s = triple_barrier(prices, start_index=0, side=1, volatility=0.01,
                       profit_mult=1.5, stop_mult=1.0, horizon_ms=10_000,
                       round_trip_cost_bps=10.0)
    assert meta_label([s]) == [1 if s.net_ret_bps > 0 else 0]


# ---- validation -----------------------------------------------------------


def test_purging_removes_overlapping_training_samples():
    """The claim the whole out-of-sample story depends on: no training sample
    may have a label window overlapping the test window."""
    n = 300
    t0 = [i * 1000 for i in range(n)]
    t1 = [t + 20_000 for t in t0]  # each label resolves 20 samples later

    cv = PurgedKFold(n_splits=5, embargo_pct=0.02)
    splits = list(cv.split(t0, t1))
    assert len(splits) == 5

    for s in splits:
        test_t0 = min(t0[i] for i in s.test_idx)
        test_t1 = max(t1[i] for i in s.test_idx)
        for i in s.train_idx:
            overlaps = t1[i] >= test_t0 and t0[i] <= test_t1
            assert not overlaps, (
                f"training sample {i} (t0={t0[i]}, t1={t1[i]}) overlaps the test "
                f"window [{test_t0}, {test_t1}] — the model can see the answer"
            )
        assert s.purged > 0, "overlapping labels existed but nothing was purged"


def test_purging_reports_when_it_removes_too_much():
    n = 200
    t0 = [i * 1000 for i in range(n)]
    t1 = [t + 500_000 for t in t0]  # absurdly long labels: everything overlaps
    splits = list(PurgedKFold(n_splits=4).split(t0, t1))
    report = split_report(splits)
    assert report["purge_share"] > 0.4
    assert "overlap too much" in report["warning"]


def test_walk_forward_never_trains_on_the_future():
    windows = walk_forward(1000, train_size=400, test_size=100, embargo=20)
    assert windows
    for w in windows:
        assert w.train_end <= w.test_start, "training window extends into the test window"
        assert w.test_start - w.train_end >= 20, "embargo not applied"
    for a, b in zip(windows, windows[1:]):
        assert b.test_start > a.test_start, "windows must move forward in time"


def test_anchored_walk_forward_grows_the_training_set():
    windows = walk_forward(1000, train_size=300, test_size=100, anchored=True)
    assert all(w.train_start == 0 for w in windows)
    assert windows[-1].train_end > windows[0].train_end


def test_too_few_samples_is_an_error_not_a_silent_bad_split():
    with pytest.raises(ValueError, match="too few samples"):
        list(PurgedKFold(n_splits=6).split([1, 2, 3], [2, 3, 4]))


# ---- calibration ----------------------------------------------------------


def test_isotonic_output_is_monotone():
    rng = random.Random(3)
    scores, outcomes = [], []
    for _ in range(500):
        s = rng.random()
        scores.append(s)
        # True probability is a compressed version of the score: the model is
        # overconfident, which is the usual direction.
        outcomes.append(1 if rng.random() < 0.2 + 0.6 * s else 0)
    cal = IsotonicCalibrator().fit(scores, outcomes)
    grid = [i / 50 for i in range(51)]
    mapped = [cal.transform(g) for g in grid]
    assert all(b >= a - 1e-9 for a, b in zip(mapped, mapped[1:])), "not monotone"
    assert all(0.0 <= m <= 1.0 for m in mapped)


def test_calibration_reduces_overconfidence():
    rng = random.Random(11)
    scores, outcomes = [], []
    for _ in range(2000):
        true_p = rng.random() * 0.5 + 0.25
        # Overconfident model: pushes probabilities toward the extremes.
        reported = min(1.0, max(0.0, (true_p - 0.5) * 2.2 + 0.5))
        scores.append(reported)
        outcomes.append(1 if rng.random() < true_p else 0)

    raw = brier_score(scores, outcomes)
    cal = IsotonicCalibrator().fit(scores, outcomes)
    calibrated = brier_score([cal.transform(s) for s in scores], outcomes)
    assert calibrated < raw, f"calibration made it worse: {calibrated} vs {raw}"


def test_calibrating_on_too_little_data_is_refused():
    with pytest.raises(ValueError, match="calibrating on this little data"):
        IsotonicCalibrator().fit([0.5] * 10, [1] * 10)


def test_brier_skill_score_detects_a_useless_model():
    """A model that always predicts the base rate has zero skill."""
    outcomes = [1] * 300 + [0] * 300
    always_half = [0.5] * 600
    report = calibration_report(always_half, outcomes)
    assert report["brier_skill_score"] == pytest.approx(0.0, abs=1e-9)


def test_expected_edge_exposes_a_bad_risk_reward_at_high_confidence():
    """The arithmetic a 'confidence > 80%' rule cannot see.

    A tight-target/wide-stop scalp at 88% confidence loses money; a 60%
    signal with a 3:1 payoff makes it. Confidence alone ranks these the wrong
    way round, which is why the risk engine gates on expected value instead.
    """
    scalp = expected_edge_bps(0.88, target_bps=5.0, stop_bps=40.0)
    swing = expected_edge_bps(0.60, target_bps=30.0, stop_bps=10.0)
    assert scalp < 0, f"expected the 88%-confidence scalp to be negative, got {scalp}"
    assert swing > 0
    assert swing > scalp


def test_reliability_curve_exposes_a_miscalibrated_bucket():
    probs = [0.85] * 100
    outcomes = [1] * 55 + [0] * 45  # claims 85%, delivers 55%
    bins = reliability_curve(probs, outcomes)
    assert len(bins) == 1
    assert bins[0].gap == pytest.approx(0.30, abs=0.01)


# ---- metrics --------------------------------------------------------------


def test_deflated_sharpe_punishes_many_trials():
    """A Sharpe found after 200 attempts is worth far less than the same
    Sharpe found on the first try."""
    rng = random.Random(5)
    returns = [rng.gauss(0.0008, 0.01) for _ in range(600)]
    one_trial = deflated_sharpe(returns, n_trials=1, periods_per_year=365 * 24)
    many_trials = deflated_sharpe(returns, n_trials=250, periods_per_year=365 * 24)
    assert many_trials < one_trial


def test_max_drawdown_finds_the_worst_peak_to_trough():
    dd, peak, trough = max_drawdown([100, 120, 90, 130, 65, 140])
    assert dd == pytest.approx(0.5, abs=1e-9)  # 130 -> 65
    assert peak == 3 and trough == 4


def test_verdict_blocks_a_strategy_whose_costs_eat_the_edge():
    result = BacktestResult(initial_equity=1000.0, n_trials=1)
    result.equity_curve = [1000.0, 1001.0, 1002.0]
    for i in range(150):
        result.trades.append(TradeRecord(
            coin="BTC", entry_ms=i * 1000, exit_ms=i * 1000 + 500, side="buy",
            entry_px=100.0, exit_px=100.1, size=1.0,
            gross_pnl=10.0, fees=9.0, funding=0.0,
        ))
    ok, reasons = result.verdict()
    assert not ok
    assert any("costs consume" in r for r in reasons)


# ---- registry -------------------------------------------------------------


def test_promotion_is_refused_without_a_shadow_period(tmp_path):
    gate = PromotionGate(min_oos_sharpe=1.0, max_brier=0.24, min_improvement=0.15,
                         shadow_hours=72)
    reg = ModelRegistry(tmp_path, gate)
    reg.register(ModelRecord(
        model_id="m1", created_ms=0, stage=ModelStage.CHALLENGER,
        metrics=ModelMetrics(oos_sharpe=2.0, deflated_sharpe=0.99, brier=0.20,
                             brier_skill_score=0.2, expected_calibration_error=0.05,
                             n_shadow=500, net_bps_per_trade=3.0),
    ))
    ok, reasons = reg.try_promote("m1")
    assert not ok
    assert any("shadow" in r for r in reasons)


def test_promotion_requires_beating_the_incumbent(tmp_path):
    import time

    gate = PromotionGate(min_oos_sharpe=1.0, max_brier=0.24, min_improvement=0.15,
                         shadow_hours=0)
    reg = ModelRegistry(tmp_path, gate)
    reg.register(ModelRecord(
        model_id="champ", created_ms=0, stage=ModelStage.CHAMPION,
        metrics=ModelMetrics(oos_sharpe=2.0), promoted_ms=1,
    ))
    reg.register(ModelRecord(
        model_id="chal", created_ms=0, stage=ModelStage.CHALLENGER,
        metrics=ModelMetrics(oos_sharpe=2.1, deflated_sharpe=0.99, brier=0.20,
                             brier_skill_score=0.2, expected_calibration_error=0.05,
                             n_shadow=500, net_bps_per_trade=3.0),
    ))
    reg.start_shadow("chal")
    ok, reasons = reg.try_promote("chal")
    assert not ok
    assert any("does not beat champion" in r for r in reasons)


def test_promoted_model_starts_at_partial_size(tmp_path):
    gate = PromotionGate(min_oos_sharpe=1.0, max_brier=0.24, min_improvement=0.15,
                         shadow_hours=0)
    reg = ModelRegistry(tmp_path, gate)
    reg.register(ModelRecord(
        model_id="m1", created_ms=0, stage=ModelStage.CHALLENGER,
        metrics=ModelMetrics(oos_sharpe=2.0, deflated_sharpe=0.99, brier=0.20,
                             brier_skill_score=0.2, expected_calibration_error=0.05,
                             n_shadow=500, net_bps_per_trade=3.0),
    ))
    reg.start_shadow("m1")
    ok, reasons = reg.try_promote("m1")
    assert ok, reasons
    assert reg.champion().capital_fraction == 0.25


def test_rollback_restores_the_previous_champion(tmp_path):
    gate = PromotionGate(min_oos_sharpe=0.0, max_brier=1.0, min_improvement=0.0,
                         shadow_hours=0, min_shadow_samples=0,
                         min_deflated_sharpe=0.0, max_calibration_error=1.0)
    reg = ModelRegistry(tmp_path, gate)
    for mid in ("old", "new"):
        reg.register(ModelRecord(
            model_id=mid, created_ms=0, stage=ModelStage.CHALLENGER,
            metrics=ModelMetrics(oos_sharpe=1.0, net_bps_per_trade=1.0,
                                 brier_skill_score=0.1),
        ))
        reg.start_shadow(mid)
        assert reg.try_promote(mid)[0]
    assert reg.champion().model_id == "new"
    restored = reg.rollback("live performance diverged from backtest")
    assert restored == "old"
    assert reg.champion().model_id == "old"
