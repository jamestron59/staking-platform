"""Command line entry point. One subcommand per phase, in the order they must
be run — the CLI is itself the runbook.

    hlq record    # Phase 0: capture. Run this for weeks before anything else.
    hlq verify    # check the recording for gaps before trusting it
    hlq backtest  # Phase 1: does the idea survive costs?
    hlq paper     # Phase 2: does the plumbing work, on live data, no capital?
    hlq live      # Phase 2 continued: same code, tiny size, real money
    hlq dataset   # Phase 3/4: build a point-in-time training set
    hlq status    # what is the bot's state right now?
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import Config, Mode
from .logging_setup import get_logger, setup_logging

log = get_logger("cli")


def _load(path: str) -> Config:
    cfg = Config.load(path)
    setup_logging(cfg.log_dir)
    return cfg


def cmd_record(args) -> int:
    from .data.recorder import Recorder

    cfg = _load(args.config)
    rec = Recorder(
        cfg.data.root, cfg.data.coins,
        rotate_minutes=cfg.data.rotate_minutes,
        intervals=cfg.data.candle_intervals,
        subscribe_book=cfg.data.subscribe_book,
        subscribe_trades=cfg.data.subscribe_trades,
        subscribe_asset_ctx=cfg.data.subscribe_asset_ctx,
        subscribe_bbo=cfg.data.subscribe_bbo,
    )
    try:
        asyncio.run(rec.run(
            cfg.network.ws_url,
            ping_interval_s=cfg.data.ws_ping_interval_s,
            max_backoff_s=cfg.data.ws_reconnect_max_backoff_s,
            staleness_budget_ms=cfg.data.max_staleness_ms,
        ))
    except KeyboardInterrupt:
        log.event("record_interrupted")
    return 0


def cmd_verify(args) -> int:
    from .data.replay import integrity_report

    cfg = _load(args.config)
    report = integrity_report(cfg.data.root)
    print(json.dumps(report, indent=2))
    if report.get("gaps"):
        print(f"\n{len(report['gaps'])} coverage gap(s). Training on this data will "
              f"produce a model whose blind spots nothing downstream can detect.",
              file=sys.stderr)
        return 1
    return 0


def cmd_backtest(args) -> int:
    from .data.replay import replay
    from .instruments import Instrument, InstrumentRegistry
    from .sim.engine import BacktestEngine
    from .strategy.baseline import FlowReversionBaseline

    cfg = _load(args.config)
    registry = _registry_from(args, cfg)
    engine = BacktestEngine(
        cfg, FlowReversionBaseline(), registry,
        n_trials=args.n_trials,
    )
    result = engine.run(replay(cfg.data.root))
    summary = result.summary()
    ok, reasons = result.verdict()

    print(json.dumps({
        "summary": summary,
        "rejections": dict(sorted(result.rejections.items(), key=lambda kv: -kv[1])),
        "deployable": ok,
        "blocking_reasons": reasons,
    }, indent=2))
    if not ok:
        print("\nNot deployable. This is the expected outcome for most ideas.",
              file=sys.stderr)
    return 0 if ok else 1


def cmd_paper(args) -> int:
    from .costs import CostModel
    from .execution.paper_gateway import PaperGateway
    from .ops.state import StateStore
    from .ops.supervisor import Supervisor
    from .strategy.baseline import FlowReversionBaseline

    cfg = _load(args.config)
    if cfg.mode is Mode.LIVE:
        print("config says mode: live — refusing to run it as paper", file=sys.stderr)
        return 2
    registry = _registry_from(args, cfg)
    costs = CostModel(
        taker_fee_bps=cfg.costs.taker_fee_bps,
        maker_fee_bps=cfg.costs.maker_fee_bps,
        residual_slippage_bps=cfg.costs.residual_slippage_bps,
    )
    gateway = PaperGateway(starting_equity=cfg.risk.equity_usd, costs=costs)
    sup = Supervisor(cfg, gateway, FlowReversionBaseline(), registry,
                     StateStore(cfg.state_dir))
    try:
        asyncio.run(sup.run())
    except KeyboardInterrupt:
        log.event("paper_interrupted")
    print(json.dumps(gateway.stats(), indent=2))
    return 0


def cmd_live(args) -> int:
    from .execution.hl_gateway import HyperliquidGateway
    from .ops.state import StateStore
    from .ops.supervisor import Supervisor
    from .strategy.baseline import FlowReversionBaseline

    cfg = _load(args.config)
    if cfg.mode is not Mode.LIVE:
        print(f"config mode is {cfg.mode.value}, not live", file=sys.stderr)
        return 2

    # A deliberate speed bump. Live trading should require an act of intent,
    # not a shell-history arrow-up.
    if not args.yes:
        print(f"\nAbout to trade REAL MONEY on {cfg.network.api_url}")
        print(f"  account          : {cfg.trading_address}")
        print(f"  subaccount route : {cfg.vault_address or '(main account)'}")
        print(f"  max position     : ${cfg.risk.max_position_notional_usd}")
        print(f"  max gross        : ${cfg.risk.max_gross_notional_usd}")
        print(f"  risk per trade   : {cfg.risk.risk_per_trade_pct}%")
        print(f"  daily loss stop  : {cfg.risk.max_daily_loss_pct}%")
        if input("\nType 'trade' to continue: ").strip() != "trade":
            print("aborted")
            return 1

    async def main() -> None:
        gateway = HyperliquidGateway(cfg)
        await gateway.verify_permissions()
        sup = Supervisor(cfg, gateway, FlowReversionBaseline(), gateway.registry,
                         StateStore(cfg.state_dir))
        await sup.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.event("live_interrupted")
    return 0


def cmd_dataset(args) -> int:
    from .model.dataset import build_dataset
    from .model.validation import PurgedKFold, split_report

    cfg = _load(args.config)
    ds = build_dataset(
        cfg.data.root, cfg.data.coins,
        sample_interval_ms=args.sample_interval * 1000,
        horizon_ms=cfg.model.horizon_ms,
        profit_mult=cfg.model.profit_take_atr_mult,
        stop_mult=cfg.model.stop_atr_mult,
        round_trip_cost_bps=2 * (cfg.costs.taker_fee_bps + cfg.costs.residual_slippage_bps),
    )
    out = {"dataset": ds.summary()}
    if len(ds) >= cfg.model.n_splits * 2:
        splits = list(PurgedKFold(cfg.model.n_splits, cfg.model.embargo_pct).split(ds.t0, ds.t1))
        out["validation"] = split_report(splits)
    if len(ds) < cfg.model.min_train_samples:
        out["warning"] = (
            f"{len(ds)} samples < {cfg.model.min_train_samples} minimum. "
            "Keep recording; a model fitted here would be fitting noise."
        )
    print(json.dumps(out, indent=2))
    return 0


def cmd_status(args) -> int:
    from .ops.state import StateStore

    cfg = _load(args.config)
    store = StateStore(cfg.state_dir)
    trades = store.read_trades()
    net = sum(t.gross_pnl - t.fees - t.funding for t in trades)
    print(json.dumps({
        "mode": cfg.mode.value,
        "killswitch": store.load_killswitch() or {"tripped": False},
        "unresolved_orders": [r.cloid for r in store.unresolved()],
        "closed_trades": len(trades),
        "net_pnl": round(net, 2),
        # Predicted vs realised, per trade. Divergence here is the earliest
        # warning that a model has stopped working.
        "edge_realisation": _edge_realisation(trades),
    }, indent=2))
    return 0


def _edge_realisation(trades) -> dict:
    if not trades:
        return {}
    expected = sum(t.expected_edge_bps for t in trades) / len(trades)
    realised = sum(t.realised_bps for t in trades) / len(trades)
    return {
        "mean_expected_bps": round(expected, 2),
        "mean_realised_bps": round(realised, 2),
        "ratio": round(realised / expected, 3) if expected else 0.0,
    }


def _registry_from(args, cfg):
    """Instrument metadata. Prefer a cached `meta.json` so backtests do not
    depend on network access, and so a historical run uses the szDecimals that
    were in force at the time."""
    from .instruments import InstrumentRegistry

    meta_path = Path(args.meta) if args.meta else Path(cfg.data.root) / "meta.json"
    if meta_path.exists():
        return InstrumentRegistry.from_meta(json.loads(meta_path.read_text()))
    from hyperliquid.info import Info

    meta = Info(cfg.network.api_url, skip_ws=True).meta()
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2))
    log.event("meta_cached", path=str(meta_path))
    return InstrumentRegistry.from_meta(meta)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hlq", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--meta", default=None, help="path to cached HL meta.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("record", help="Phase 0: capture market data").set_defaults(fn=cmd_record)
    sub.add_parser("verify", help="check recording integrity").set_defaults(fn=cmd_verify)

    bt = sub.add_parser("backtest", help="Phase 1: replay with costs")
    bt.add_argument("--n-trials", type=int, default=1,
                    help="how many strategy variants you have tried in total; "
                         "used to deflate the Sharpe ratio. Be honest.")
    bt.set_defaults(fn=cmd_backtest)

    sub.add_parser("paper", help="Phase 2: live data, simulated fills").set_defaults(fn=cmd_paper)

    lv = sub.add_parser("live", help="Phase 2: real money, tiny size")
    lv.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    lv.set_defaults(fn=cmd_live)

    ds = sub.add_parser("dataset", help="Phase 4: build a point-in-time dataset")
    ds.add_argument("--sample-interval", type=int, default=60, help="seconds between samples")
    ds.set_defaults(fn=cmd_dataset)

    sub.add_parser("status", help="current bot state").set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
