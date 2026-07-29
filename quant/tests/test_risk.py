"""Risk engine and kill switches.

The tests that matter most are the ones asserting a *refusal*: a risk engine
that never says no is decoration.
"""

from __future__ import annotations

import pytest

from hlq.config import RiskConfig
from hlq.costs import CostModel
from hlq.risk.engine import RiskEngine
from hlq.risk.killswitch import KillSwitch, TripReason
from hlq.risk.liquidation import assess, estimate_liquidation_px, maintenance_margin_fraction
from hlq.types import OrderIntent, Position, Side

from .conftest import make_book


@pytest.fixture
def cfg() -> RiskConfig:
    return RiskConfig(
        equity_usd=1000.0, risk_per_trade_pct=0.5, max_portfolio_risk_pct=1.5,
        assumed_correlation=0.8, max_position_notional_usd=500.0,
        max_gross_notional_usd=1000.0, max_leverage=3.0, max_concurrent_positions=2,
        min_edge_after_costs_bps=2.0, max_spread_bps=5.0,
        min_depth_usd_at_10bps=10_000.0, min_liquidation_buffer_mult=3.0,
    )


@pytest.fixture
def kill(cfg) -> KillSwitch:
    return KillSwitch(
        max_daily_loss_pct=cfg.max_daily_loss_pct, max_drawdown_pct=cfg.max_drawdown_pct,
        max_consecutive_losses=cfg.max_consecutive_losses,
        max_reject_rate=cfg.max_reject_rate, reject_window=cfg.reject_window,
        max_staleness_ms=5000,
    )


@pytest.fixture
def engine(cfg, kill) -> RiskEngine:
    return RiskEngine(cfg, CostModel(taker_fee_bps=4.5, maker_fee_bps=1.5), kill)


def _intent(edge_bps: float = 40.0, stop_pct: float = 0.01) -> OrderIntent:
    mid = 50_000.0
    return OrderIntent(
        coin="BTC", side=Side.BUY, target_notional_usd=300.0, limit_px=mid,
        stop_px=mid * (1 - stop_pct), take_profit_px=mid * (1 + stop_pct * 2),
        expected_edge_bps=edge_bps, confidence=0.7, reason="test",
    )


def test_size_comes_from_the_stop_not_the_requested_notional(engine, btc, book):
    """0.5% of $1000 equity = $5 risk. With a 1% stop on a $50k price, that is
    $500 of notional — the requested $300 is irrelevant to sizing."""
    d = engine.evaluate(_intent(), btc, book, {})
    assert d.approved, d.reason
    assert d.notional_usd == pytest.approx(500.0, rel=0.02)


def test_wider_stop_produces_a_smaller_position(engine, btc, book):
    tight = engine.evaluate(_intent(stop_pct=0.005), btc, book, {})
    wide = engine.evaluate(_intent(stop_pct=0.02), btc, book, {})
    assert tight.approved and wide.approved
    assert wide.size < tight.size


def test_edge_below_cost_is_refused(engine, btc, book):
    """The gate the original design lacked: a confidence threshold alone
    admits trades that cannot pay for themselves."""
    d = engine.evaluate(_intent(edge_bps=3.0), btc, book, {})
    assert not d.approved
    assert d.reason == "edge_does_not_cover_costs"
    assert d.diagnostics["net_bps"] < 2.0


def test_wide_spread_is_refused(engine, btc):
    wide = make_book("BTC", 1000, 50_000.0, spread=100.0, depth=50.0)
    d = engine.evaluate(_intent(), btc, wide, {})
    assert not d.approved and d.reason == "spread_too_wide"


def test_thin_book_is_refused(engine, btc):
    thin = make_book("BTC", 1000, 50_000.0, spread=1.0, depth=0.001)
    d = engine.evaluate(_intent(), btc, thin, {})
    assert not d.approved and d.reason == "insufficient_depth"


def test_position_notional_cap_is_hard(cfg, kill, btc, book):
    cfg.max_position_notional_usd = 100.0
    e = RiskEngine(cfg, CostModel(taker_fee_bps=4.5, maker_fee_bps=1.5), kill)
    d = e.evaluate(_intent(), btc, book, {})
    assert d.approved
    assert d.notional_usd <= 100.0 + 1e-6


def test_correlated_positions_consume_the_portfolio_budget(engine, btc, book):
    """Five 0.5% positions are not 2.5% of independent risk. With correlation
    0.8 they are close to fully additive, and the budget must reflect that."""
    alone = engine.evaluate(_intent(), btc, book, {})
    assert alone.approved

    crowded = {
        "ETH": Position(coin="ETH", size=2.0, entry_px=3000.0),
        "SOL": Position(coin="SOL", size=40.0, entry_px=150.0),
    }
    with_others = engine.evaluate(_intent(), btc, book, crowded)
    assert (not with_others.approved) or with_others.size < alone.size, (
        "existing correlated exposure did not reduce the new position"
    )


def test_max_concurrent_positions_is_enforced(cfg, kill, btc, book):
    cfg.max_concurrent_positions = 1
    e = RiskEngine(cfg, CostModel(taker_fee_bps=4.5, maker_fee_bps=1.5), kill)
    held = {"ETH": Position(coin="ETH", size=0.1, entry_px=3000.0)}
    d = e.evaluate(_intent(), btc, book, held, marks={"ETH": 3000.0})
    assert not d.approved and d.reason == "max_concurrent_positions_reached"


def test_other_positions_are_valued_at_their_own_price(cfg, kill, btc, book):
    """Regression: gross notional once valued every position at the price of
    the coin under evaluation, so 0.1 ETH counted as 0.1 x the BTC price."""
    cfg.max_concurrent_positions = 5
    cfg.max_gross_notional_usd = 1000.0
    e = RiskEngine(cfg, CostModel(taker_fee_bps=4.5, maker_fee_bps=1.5), kill)
    held = {"ETH": Position(coin="ETH", size=0.1, entry_px=3000.0)}  # $300

    d = e.evaluate(_intent(), btc, book, held, marks={"ETH": 3000.0})
    assert d.approved, d.reason
    # $1000 gross cap minus $300 of ETH leaves $700 of room.
    assert d.notional_usd <= 700.0 + 1e-6


def test_tripped_kill_switch_blocks_new_risk_but_allows_reducing(engine, kill, btc, book):
    kill.trip_manual(TripReason.MANUAL, "test", 1000)
    assert not engine.evaluate(_intent(), btc, book, {}).approved

    reduce = OrderIntent(coin="BTC", side=Side.SELL, target_notional_usd=100.0,
                         limit_px=50_000.0, reduce_only=True, reason="exit")
    assert engine.evaluate(reduce, btc, book, {}).approved


# ---- liquidation ----------------------------------------------------------


def test_maintenance_margin_is_half_of_max_leverage_initial():
    assert maintenance_margin_fraction(40) == pytest.approx(1 / 80)


def test_long_liquidation_price_sits_below_entry():
    liq = estimate_liquidation_px(entry_px=50_000.0, size=0.1, side=Side.BUY,
                                  margin_available=500.0, max_leverage=40)
    assert liq is not None and liq < 50_000.0


def test_stop_inside_the_liquidation_buffer_is_rejected():
    """A stop that sits close to liquidation is not a stop: a wick liquidates
    the position before the stop fills."""
    tight = assess(entry_px=50_000.0, stop_px=49_000.0, size=1.0, side=Side.BUY,
                   margin_available=1_200.0, max_leverage=40, min_buffer_mult=3.0)
    assert not tight.survivable
    assert "liquidation" in tight.note

    safe = assess(entry_px=50_000.0, stop_px=49_500.0, size=0.05, side=Side.BUY,
                  margin_available=5_000.0, max_leverage=40, min_buffer_mult=3.0)
    assert safe.survivable


def test_exchange_reported_liquidation_price_wins():
    """Our model is an estimate; HL's number is authoritative."""
    r = assess(entry_px=50_000.0, stop_px=49_500.0, size=0.05, side=Side.BUY,
               margin_available=5_000.0, max_leverage=40, min_buffer_mult=3.0,
               known_liquidation_px=49_600.0)
    assert r.liquidation_px == 49_600.0
    assert not r.survivable  # exchange says liquidation is inside our stop


def test_stop_on_the_wrong_side_is_a_config_error():
    r = assess(entry_px=50_000.0, stop_px=51_000.0, size=0.05, side=Side.BUY,
               margin_available=5_000.0, max_leverage=40, min_buffer_mult=3.0)
    assert not r.survivable and "wrong side" in r.note


# ---- kill switches --------------------------------------------------------


def test_staleness_trips_but_does_not_force_market_orders(kill):
    """Flattening into a feed we cannot see is worse than holding: stops rest
    on the exchange, not in this process."""
    ev = kill.check_staleness(30_000, 1000)
    assert ev is not None and ev.reason is TripReason.DATA_STALE
    assert not ev.require_flat
    assert kill.tripped


def test_trip_is_sticky_until_a_human_resets(kill):
    kill.check_staleness(30_000, 1000)
    assert kill.check_staleness(0, 2000) is None
    assert kill.tripped, "cleared itself once the condition passed"
    kill.reset("operator checked the feed")
    assert not kill.tripped


def test_losing_streak_trips_and_a_win_resets_the_count(kill):
    for i in range(3):
        assert kill.record_trade_result(-10.0, 1000 + i) is None
    kill.record_trade_result(5.0, 2000)
    assert kill.state.consecutive_losses == 0
    for i in range(4):
        ev = kill.record_trade_result(-10.0, 3000 + i)
    assert ev is not None and ev.reason is TripReason.LOSS_STREAK


def test_daily_loss_forces_a_flatten(kill):
    kill.check_equity(1000.0, 1_700_000_000_000)
    ev = kill.check_equity(970.0, 1_700_000_001_000)
    assert ev is not None and ev.reason is TripReason.DAILY_LOSS
    assert ev.require_flat and kill.must_flatten


def test_reject_rate_trips_only_once_the_window_is_full(kill):
    for i in range(19):
        assert kill.record_order_outcome(True, i) is None, "tripped on a partial window"
    ev = kill.record_order_outcome(True, 20)
    assert ev is not None and ev.reason is TripReason.REJECT_RATE


def test_position_divergence_trips(kill):
    ev = kill.check_divergence("BTC", local_size=0.0, exchange_size=0.4, now_ms=1000)
    assert ev is not None and ev.reason is TripReason.STATE_DIVERGENCE
