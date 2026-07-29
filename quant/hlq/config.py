"""Typed configuration with safety interlocks.

Going live is not a config flag you can flip by accident. `mode: live` alone is
not enough — it additionally requires an explicit acknowledgement key, a
non-zero set of hard notional caps, and (recommended) a dedicated subaccount
address. `validate()` raises on boot rather than degrading to a default,
because every "sensible default" in a trading system is a way to lose money
quietly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, get_args, get_origin

import yaml

MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"
MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS = "wss://api.hyperliquid-testnet.xyz/ws"


class Mode(str, Enum):
    RECORD = "record"  # capture only, never authenticates
    BACKTEST = "backtest"
    PAPER = "paper"  # live data, simulated fills
    LIVE = "live"


class ConfigError(Exception):
    pass


@dataclass
class NetworkConfig:
    testnet: bool = False

    @property
    def api_url(self) -> str:
        return TESTNET_API if self.testnet else MAINNET_API

    @property
    def ws_url(self) -> str:
        return TESTNET_WS if self.testnet else MAINNET_WS


@dataclass
class AccountConfig:
    # The address that owns the funds and positions we manage. For a subaccount
    # setup this is the SUBACCOUNT address, not the master wallet.
    account_address: str = ""
    # HL routes actions for a subaccount through the `vaultAddress` field. Set
    # this to the subaccount address when trading a subaccount from the master
    # key or an approved agent. Leave empty when trading the main account.
    subaccount_address: str = ""
    # Name of the env var holding the private key. The key itself is NEVER in
    # config or on disk. Use an approved agent (API) wallet: it can trade but
    # cannot withdraw, which caps the blast radius of a compromised host.
    private_key_env: str = "HL_API_SECRET"
    require_agent_wallet: bool = True
    # A subaccount is an ACCOUNT-LEVEL bound on what this bot can ever touch —
    # stronger than any config value, because the bot cannot raise it at
    # runtime. HL gates subaccount creation behind traded volume, so a new
    # account cannot have one. Running on the main account is therefore
    # legitimate, but it removes that bound, and the substitute is to keep only
    # the risk capital in the HL account and the rest off-exchange.
    # Setting this to true is an acknowledgement that you have done so.
    no_subaccount_acknowledged: bool = False

    def load_secret(self) -> str:
        key = os.environ.get(self.private_key_env, "").strip()
        if not key:
            raise ConfigError(
                f"env var {self.private_key_env} is empty; refusing to start a trading session"
            )
        if not key.startswith("0x") or len(key) != 66:
            raise ConfigError(f"{self.private_key_env} does not look like a 32-byte hex key")
        return key


@dataclass
class DataConfig:
    coins: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    candle_intervals: list[str] = field(default_factory=lambda: ["1m", "5m", "15m", "1h"])
    subscribe_book: bool = True
    subscribe_trades: bool = True
    subscribe_asset_ctx: bool = True
    subscribe_bbo: bool = False  # higher rate; enable once storage is proven
    root: str = "data"
    rotate_minutes: int = 60
    # Hard staleness budget. Beyond this the bot is blind and must stop trading.
    max_staleness_ms: int = 5_000
    ws_ping_interval_s: int = 20
    ws_reconnect_max_backoff_s: int = 60


@dataclass
class CostConfig:
    """Set these from your ACTUAL fee tier on HL, not from the public headline
    numbers. Under-stating fees is the single most common way a backtest turns
    a losing strategy into a winner."""

    taker_fee_bps: float = 4.5
    maker_fee_bps: float = 1.5  # negative if you earn a rebate
    # Residual slippage beyond what walking the book explains: queue position,
    # latency, adverse selection. Calibrate from realised fills, see ops/metrics.
    residual_slippage_bps: float = 1.0
    funding_interval_hours: float = 1.0


@dataclass
class RiskConfig:
    equity_usd: float = 1_000.0
    risk_per_trade_pct: float = 0.5  # of equity, at the stop
    max_portfolio_risk_pct: float = 1.5  # correlation-adjusted, aggregate
    assumed_correlation: float = 0.8  # crypto perps move together; be honest
    max_position_notional_usd: float = 500.0
    max_gross_notional_usd: float = 1_500.0
    max_leverage: float = 3.0
    max_concurrent_positions: int = 2

    # Kill switches
    max_daily_loss_pct: float = 2.0
    max_drawdown_pct: float = 5.0
    max_consecutive_losses: int = 4
    max_reject_rate: float = 0.25  # over the rolling order window
    reject_window: int = 20
    # Refuse to hold a stop closer than this fraction of the distance to
    # liquidation — a stop inside the liquidation buffer is not a stop.
    min_liquidation_buffer_mult: float = 3.0

    # Signal gating. `min_edge_after_costs_bps` is the one that matters: a
    # confidence threshold alone selects for frequent, small, cost-negative trades.
    min_edge_after_costs_bps: float = 2.0
    max_spread_bps: float = 3.0
    min_depth_usd_at_10bps: float = 25_000.0


@dataclass
class ExecutionConfig:
    prefer_maker: bool = True
    maker_timeout_ms: int = 3_000  # then requote or cross
    max_requotes: int = 3
    # Slice when our size would eat more than this share of visible depth.
    max_participation_of_depth: float = 0.10
    slice_interval_ms: int = 750
    max_slices: int = 5
    ioc_slippage_bps: float = 5.0  # marketable-limit protection, never a true market order
    # Dead man's switch: HL cancels all resting orders if we stop refreshing.
    # HL allows a max of 10 triggers per UTC day and requires >=5s lead time.
    dead_man_switch_s: int = 60
    dead_man_refresh_s: int = 20
    # Actions are signed with an expiry so a delayed packet cannot execute late.
    action_expiry_ms: int = 10_000
    reconcile_interval_s: int = 15
    order_timeout_ms: int = 8_000


@dataclass
class ModelConfig:
    horizon_ms: int = 300_000  # 5 minutes
    profit_take_atr_mult: float = 1.5
    stop_atr_mult: float = 1.0
    embargo_pct: float = 0.01
    n_splits: int = 6
    min_train_samples: int = 5_000
    # Promotion gates — a challenger must clear all of these to take capital.
    min_oos_sharpe: float = 1.0
    max_calibration_brier: float = 0.24
    min_challenger_improvement: float = 0.15
    shadow_period_hours: int = 72


@dataclass
class AlertConfig:
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    enabled: bool = False


@dataclass
class Config:
    mode: Mode = Mode.RECORD
    network: NetworkConfig = field(default_factory=NetworkConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    data: DataConfig = field(default_factory=DataConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    log_dir: str = "logs"
    state_dir: str = "state"

    # Explicit acknowledgement. `mode: live` without this is refused.
    i_understand_this_trades_real_money: bool = False

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Config":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        cfg = _from_mapping(cls, raw)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        r, e = self.risk, self.execution

        if r.risk_per_trade_pct <= 0 or r.risk_per_trade_pct > 5:
            raise ConfigError("risk_per_trade_pct must be in (0, 5]")
        if r.max_portfolio_risk_pct < r.risk_per_trade_pct:
            raise ConfigError("max_portfolio_risk_pct below risk_per_trade_pct")
        if r.max_position_notional_usd > r.max_gross_notional_usd:
            raise ConfigError("max_position_notional_usd exceeds max_gross_notional_usd")
        if not 0 <= r.assumed_correlation <= 1:
            raise ConfigError("assumed_correlation must be in [0, 1]")
        if r.min_liquidation_buffer_mult < 1.5:
            raise ConfigError("min_liquidation_buffer_mult below 1.5 is not a buffer")
        if not self.data.coins:
            raise ConfigError("no coins configured")

        if e.dead_man_switch_s < 5:
            raise ConfigError("HL requires the dead-man switch at least 5s in the future")
        if e.dead_man_refresh_s >= e.dead_man_switch_s:
            raise ConfigError("dead_man_refresh_s must be well below dead_man_switch_s")
        if e.max_participation_of_depth <= 0 or e.max_participation_of_depth > 0.5:
            raise ConfigError("max_participation_of_depth must be in (0, 0.5]")

        if self.mode is Mode.LIVE:
            if not self.i_understand_this_trades_real_money:
                raise ConfigError(
                    "mode=live requires i_understand_this_trades_real_money: true"
                )
            if not self.account.account_address:
                raise ConfigError("mode=live requires account.account_address")
            if r.max_position_notional_usd <= 0 or r.max_gross_notional_usd <= 0:
                raise ConfigError("mode=live requires non-zero hard notional caps")
            if not self.account.subaccount_address and not self.account.no_subaccount_acknowledged:
                raise ConfigError(
                    "no subaccount configured. Without one, the bot's exposure is bounded "
                    "only by its own config, not by an account boundary. Either set "
                    "account.subaccount_address, or keep only your risk capital in the HL "
                    "account and set account.no_subaccount_acknowledged: true"
                )
            # At small equity the risk-per-trade rule can produce orders below
            # HL's minimum notional, which shows up as a bot that silently never
            # trades. Surface it at boot instead.
            if r.equity_usd > 0 and r.max_position_notional_usd > r.equity_usd * r.max_leverage:
                raise ConfigError(
                    f"max_position_notional_usd ({r.max_position_notional_usd}) exceeds "
                    f"equity x leverage ({r.equity_usd * r.max_leverage})"
                )
            self.account.load_secret()  # fail at boot, not at the first order

        if self.mode in (Mode.LIVE, Mode.PAPER) and self.data.max_staleness_ms > 30_000:
            raise ConfigError("max_staleness_ms above 30s is not a staleness guard")

    @property
    def trading_address(self) -> str:
        """Address whose positions we manage."""
        return self.account.subaccount_address or self.account.account_address

    @property
    def vault_address(self) -> Optional[str]:
        """Passed to the SDK's `Exchange(vault_address=...)`. HL routes
        subaccount actions through this field."""
        return self.account.subaccount_address or None


def _from_mapping(dc_type: type, raw: Mapping[str, Any]) -> Any:
    """Strict dataclass hydration: unknown keys are an error.

    A typo like `max_drawdown_pc:` silently falling back to the default is
    exactly the class of bug that shows up as an unexplained loss weeks later.
    """
    if not is_dataclass(dc_type):
        return raw
    known = {f.name: f for f in fields(dc_type)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown config keys for {dc_type.__name__}: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, f in known.items():
        if name not in raw:
            continue
        value = raw[name]
        ftype = f.type
        if isinstance(ftype, str):  # from __future__ annotations
            ftype = {
                "Mode": Mode,
                "NetworkConfig": NetworkConfig,
                "AccountConfig": AccountConfig,
                "DataConfig": DataConfig,
                "CostConfig": CostConfig,
                "RiskConfig": RiskConfig,
                "ExecutionConfig": ExecutionConfig,
                "ModelConfig": ModelConfig,
                "AlertConfig": AlertConfig,
            }.get(ftype, None)
        if ftype is Mode:
            kwargs[name] = Mode(value)
        elif ftype is not None and is_dataclass(ftype):
            kwargs[name] = _from_mapping(ftype, value or {})
        else:
            kwargs[name] = value
    return dc_type(**kwargs)
