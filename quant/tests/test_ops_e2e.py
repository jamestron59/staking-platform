"""State persistence, reconciliation, and an end-to-end backtest.

The end-to-end test is not about profitability — the baseline strategy is
deliberately weak. It asserts that the whole chain runs, that costs are
actually charged, and that a losing strategy is reported as losing rather than
quietly turning a profit somewhere in the accounting.
"""

from __future__ import annotations

import json

import pytest

from hlq.config import Config, ConfigError, Mode
from hlq.execution.gateway import AccountSnapshot, new_cloid
from hlq.execution.reconciler import Reconciler
from hlq.ops.state import OrderRecord, ShadowJournal, StateStore, TradeAttribution, atomic_write_json
from hlq.risk.killswitch import KillSwitch
from hlq.sim.engine import BacktestEngine
from hlq.strategy.baseline import FlowReversionBaseline
from hlq.types import Order, OrderStatus, Position, Side, TimeInForce

from .conftest import synthetic_events


# ---- cloid / idempotency --------------------------------------------------


def test_cloid_format_matches_hl_requirements():
    """HL requires 0x + 32 hex chars. The SDK's Cloid class rejects anything
    else, and a malformed one fails at signing time — mid-order."""
    from hyperliquid.utils.signing import Cloid

    for _ in range(50):
        c = new_cloid()
        assert c.startswith("0x") and len(c) == 34
        Cloid.from_str(c)  # raises if invalid


def test_cloids_are_unique():
    assert len({new_cloid() for _ in range(5000)}) == 5000


# ---- state ----------------------------------------------------------------


def test_atomic_write_leaves_the_old_version_on_failure(tmp_path, monkeypatch):
    """A crash mid-write must leave the previous good version, never a
    half-written file that reads as valid-but-wrong state."""
    import os

    path = tmp_path / "s.json"
    atomic_write_json(path, {"v": 1})
    assert json.loads(path.read_text())["v"] == 1

    def boom(*args, **kwargs):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(path, {"v": 2})

    assert json.loads(path.read_text())["v"] == 1, "old version was destroyed"
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind after failure"


def test_orders_survive_a_restart(tmp_path):
    store = StateStore(tmp_path)
    cloid = new_cloid()
    store.record_order(OrderRecord(
        cloid=cloid, coin="BTC", side="buy", sz=0.01, limit_px=50_000.0,
        intent_reason="test", stop_px=49_500.0, created_ms=1000,
    ))
    reborn = StateStore(tmp_path)
    assert cloid in reborn.orders
    assert reborn.unresolved()[0].intent_reason == "test"


def test_corrupt_state_raises_rather_than_looking_empty(tmp_path):
    """An empty state file reads to the reconciler as 'no open orders', which
    is the most dangerous possible misreading."""
    (tmp_path / "orders.json").write_text("{ this is not json")
    with pytest.raises(Exception):
        StateStore(tmp_path)


def test_trade_journal_is_append_only(tmp_path):
    store = StateStore(tmp_path)
    for i in range(3):
        store.append_trade(TradeAttribution(
            coin="BTC", entry_ms=i, exit_ms=i + 1, side="buy", entry_px=100.0,
            exit_px=101.0, size=1.0, gross_pnl=1.0, fees=0.1, funding=0.0,
            entry_reason="r", exit_reason="target", confidence=0.7,
            expected_edge_bps=10.0, realised_bps=100.0, features={"a": 1.0},
        ))
    assert len(StateStore(tmp_path).read_trades()) == 3


def test_shadow_journal_records_rejected_signals(tmp_path):
    """Without this the retraining step only ever sees trades that passed the
    filters — it can never learn that a filter is harmful."""
    j = ShadowJournal(tmp_path)
    j.record(ts_ms=1, coin="BTC", side="buy", confidence=0.9,
             expected_edge_bps=3.0, rejected_by="risk",
             reason="edge_does_not_cover_costs", mid=50_000.0,
             features={"flow": 0.4})
    lines = (tmp_path / "shadow_signals.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["reason"] == "edge_does_not_cover_costs"


# ---- reconciliation -------------------------------------------------------


@pytest.fixture
def kill() -> KillSwitch:
    return KillSwitch(max_daily_loss_pct=2, max_drawdown_pct=5,
                      max_consecutive_losses=4, max_reject_rate=0.25,
                      reject_window=20, max_staleness_ms=5000)


def test_phantom_position_is_detected(tmp_path, kill):
    r = Reconciler(StateStore(tmp_path), kill)
    r.set_local({})
    snap = AccountSnapshot(
        equity_usd=1000.0,
        positions={"BTC": Position(coin="BTC", size=0.4, entry_px=50_000.0)},
        at_ms=1000,
    )
    result = r.reconcile(snap, startup=False)
    assert any(d.kind == "phantom_position" for d in result.divergences)
    assert kill.tripped


def test_missing_position_is_detected(tmp_path, kill):
    r = Reconciler(StateStore(tmp_path), kill)
    r.set_local({"BTC": Position(coin="BTC", size=0.4, entry_px=50_000.0)})
    result = r.reconcile(AccountSnapshot(equity_usd=1000.0, at_ms=1000), startup=False)
    assert any(d.kind == "missing_position" for d in result.divergences)


def test_size_mismatch_is_detected(tmp_path, kill):
    r = Reconciler(StateStore(tmp_path), kill)
    r.set_local({"BTC": Position(coin="BTC", size=0.4, entry_px=50_000.0)})
    snap = AccountSnapshot(
        equity_usd=1000.0,
        positions={"BTC": Position(coin="BTC", size=0.25, entry_px=50_000.0)},
        at_ms=1000,
    )
    result = r.reconcile(snap, startup=False)
    assert any(d.kind == "size_mismatch" for d in result.divergences)


def test_unknown_resting_order_is_fatal(tmp_path, kill):
    """An order we did not place means something else is trading this
    account. Continuing would compound the problem."""
    r = Reconciler(StateStore(tmp_path), kill)
    r.set_local({})
    snap = AccountSnapshot(
        equity_usd=1000.0,
        open_orders=[Order(cloid="", coin="BTC", side=Side.BUY, sz=1.0,
                           limit_px=50_000.0, tif=TimeInForce.GTC, oid=99)],
        at_ms=1000,
    )
    result = r.reconcile(snap, startup=False)
    assert result.fatal


def test_our_own_orders_are_not_flagged(tmp_path, kill):
    store = StateStore(tmp_path)
    cloid = new_cloid()
    store.record_order(OrderRecord(cloid=cloid, coin="BTC", side="buy", sz=1.0,
                                   limit_px=50_000.0, intent_reason="t", created_ms=1))
    r = Reconciler(store, kill)
    r.set_local({})
    snap = AccountSnapshot(
        equity_usd=1000.0,
        open_orders=[Order(cloid=cloid, coin="BTC", side=Side.BUY, sz=1.0,
                           limit_px=50_000.0, tif=TimeInForce.GTC, oid=99)],
        at_ms=1000,
    )
    assert not r.reconcile(snap, startup=False).fatal


def test_exchange_truth_is_adopted(tmp_path, kill):
    r = Reconciler(StateStore(tmp_path), kill)
    r.set_local({"BTC": Position(coin="BTC", size=0.4, entry_px=50_000.0)})
    snap = AccountSnapshot(
        equity_usd=1000.0,
        positions={"BTC": Position(coin="BTC", size=0.25, entry_px=50_100.0)},
        at_ms=1000,
    )
    result = r.reconcile(snap, startup=False)
    assert result.positions["BTC"].size == 0.25


# ---- config safety --------------------------------------------------------


def _write_cfg(tmp_path, **overrides) -> str:
    import yaml

    base = {
        "mode": "live",
        "account": {"account_address": "0x" + "1" * 40},
        "risk": {"max_position_notional_usd": 100.0, "max_gross_notional_usd": 200.0},
        "i_understand_this_trades_real_money": True,
    }
    base.update(overrides)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(base))
    return str(p)


def test_live_mode_requires_explicit_acknowledgement(tmp_path):
    path = _write_cfg(tmp_path, i_understand_this_trades_real_money=False)
    with pytest.raises(ConfigError, match="i_understand_this_trades_real_money"):
        Config.load(path)


def test_live_mode_requires_a_key_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HL_API_SECRET", raising=False)
    with pytest.raises(ConfigError, match="is empty"):
        Config.load(_write_cfg(tmp_path))


def test_unknown_config_key_is_an_error(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"mode": "record", "risk": {"max_drawdown_pc": 5.0}}))
    with pytest.raises(ConfigError, match="unknown config keys"):
        Config.load(str(p))


def test_dead_man_switch_below_hl_minimum_is_rejected(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"mode": "record", "execution": {"dead_man_switch_s": 3}}))
    with pytest.raises(ConfigError, match="at least 5s"):
        Config.load(str(p))


def test_subaccount_routes_through_vault_address(tmp_path):
    import yaml

    sub = "0x" + "2" * 40
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({
        "mode": "record",
        "account": {"account_address": "0x" + "1" * 40, "subaccount_address": sub},
    }))
    cfg = Config.load(str(p))
    assert cfg.vault_address == sub
    assert cfg.trading_address == sub


# ---- end to end -----------------------------------------------------------


def test_backtest_runs_end_to_end_and_charges_costs(registry):
    cfg = Config()
    cfg.data.coins = ["BTC"]
    cfg.risk.equity_usd = 1000.0
    cfg.risk.min_depth_usd_at_10bps = 1000.0
    cfg.risk.max_spread_bps = 50.0
    cfg.risk.min_edge_after_costs_bps = -1e9  # let trades through to exercise the chain

    engine = BacktestEngine(cfg, FlowReversionBaseline(), registry, n_trials=1)
    result = engine.run(iter(synthetic_events(n=12_000)))
    summary = result.summary()

    assert summary["n_trades"] > 0, (
        f"no trades executed; rejections were {result.rejections}"
    )
    assert summary["fees_paid"] > 0, "trades happened but no fees were charged"
    # Random-walk data has no edge, so gross should be near zero and net should
    # be meaningfully worse. A backtest showing profit here would mean the
    # accounting is manufacturing it.
    assert summary["net_pnl"] < summary["gross_pnl"]

    ok, reasons = result.verdict()
    assert not ok and reasons, "a strategy with no edge was reported as deployable"


def test_cost_gate_refuses_signals_that_cannot_pay_for_themselves(registry):
    """Raise the required net edge above anything this strategy claims, and
    every intent must be refused with that reason rather than traded.

    This is the gate the original architecture lacked: a confidence threshold
    admits trades whose target cannot clear the round trip.
    """
    cfg = Config()
    cfg.data.coins = ["BTC"]
    cfg.risk.min_depth_usd_at_10bps = 1000.0
    cfg.risk.max_spread_bps = 50.0
    cfg.risk.min_edge_after_costs_bps = 500.0  # unreachable

    engine = BacktestEngine(cfg, FlowReversionBaseline(), registry)
    result = engine.run(iter(synthetic_events(n=12_000)))

    assert result.rejections, "no intents were evaluated at all"
    assert "edge_does_not_cover_costs" in result.rejections
    assert result.summary()["n_trades"] == 0, "traded despite failing the cost gate"


def test_market_quality_gates_refuse_a_wide_thin_market(registry):
    cfg = Config()
    cfg.data.coins = ["BTC"]
    cfg.risk.max_spread_bps = 0.001          # nothing can be tight enough
    cfg.risk.min_edge_after_costs_bps = -1e9

    engine = BacktestEngine(cfg, FlowReversionBaseline(), registry)
    result = engine.run(iter(synthetic_events(n=12_000)))
    assert result.rejections.get("spread_too_wide", 0) > 0
    assert result.summary()["n_trades"] == 0
