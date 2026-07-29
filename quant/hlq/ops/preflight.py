"""Configuration self-consistency checks.

At $100 of equity the constraints interact in ways that are invisible in the
config file and fatal in operation. Two specific traps:

**The bot that never trades.** Risk-per-trade sizing computes
`notional = risk_usd / stop_fraction`. With $100 equity and 0.5% risk that is
$0.50 / stop_fraction. A 6% stop asks for an $8 order, HL's minimum is $10, the
order is vetoed — every time. The bot runs for a week, logs nothing but
`below_min_notional`, and looks like it is working.

**The risk number that is fiction.** When the position cap binds, actual risk
at the stop is far below the configured percentage. "0.5% per trade" then
describes nothing real, and the drawdown limits are calibrated against a
quantity that never occurs.

Neither is a bug. Both are arithmetic that must be stated out loud before real
money is involved, which is what this module does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import Config


@dataclass
class Finding:
    level: str  # "ok" | "warn" | "block"
    check: str
    detail: str


@dataclass
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.level == "block" for f in self.findings)

    def add(self, level: str, check: str, detail: str) -> None:
        self.findings.append(Finding(level, check, detail))

    def as_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "findings": [
                {"level": f.level, "check": f.check, "detail": f.detail}
                for f in self.findings
            ],
        }


def preflight(cfg: Config, *, min_notional_usd: float = 10.0) -> PreflightReport:
    r = cfg.risk
    rep = PreflightReport()
    risk_usd = r.equity_usd * r.risk_per_trade_pct / 100.0

    # ---- can this configuration ever produce a legal order? ---------------

    if risk_usd <= 0:
        rep.add("block", "risk_budget", "risk per trade computes to zero")
        return rep

    max_viable_stop_pct = 100.0 * risk_usd / min_notional_usd
    if max_viable_stop_pct < 0.5:
        rep.add(
            "block", "min_notional_unreachable",
            f"risk budget is ${risk_usd:.2f}/trade; any stop wider than "
            f"{max_viable_stop_pct:.2f}% produces an order below HL's "
            f"${min_notional_usd:.0f} minimum. At this equity almost every signal "
            f"will be vetoed and the bot will appear to run while never trading. "
            f"Raise risk_per_trade_pct or equity.",
        )
    elif max_viable_stop_pct < 3.0:
        rep.add(
            "warn", "min_notional_tight",
            f"stops wider than {max_viable_stop_pct:.2f}% will be rejected for "
            f"falling below the ${min_notional_usd:.0f} minimum order size",
        )
    else:
        rep.add(
            "ok", "min_notional",
            f"stops up to {max_viable_stop_pct:.1f}% stay above the minimum order size",
        )

    # ---- does the notional cap make the stated risk fictional? ------------

    cap_binds_below_pct = 100.0 * risk_usd / r.max_position_notional_usd
    if cap_binds_below_pct > 0:
        rep.add(
            "warn" if cap_binds_below_pct > 1.0 else "ok",
            "effective_risk",
            f"for stops tighter than {cap_binds_below_pct:.2f}% the "
            f"${r.max_position_notional_usd:.0f} position cap binds first, so actual "
            f"risk at the stop is below the configured {r.risk_per_trade_pct}%. "
            f"The cap, not the risk rule, is what sizes those trades.",
        )

    # ---- are the caps internally consistent? ------------------------------

    if r.max_position_notional_usd < min_notional_usd:
        rep.add(
            "block", "position_cap_below_minimum",
            f"max_position_notional_usd (${r.max_position_notional_usd:.0f}) is below "
            f"HL's ${min_notional_usd:.0f} minimum: no order can ever be placed",
        )

    max_concurrent_notional = r.max_position_notional_usd * r.max_concurrent_positions
    if max_concurrent_notional > r.max_gross_notional_usd:
        rep.add(
            "warn", "gross_cap_binds",
            f"{r.max_concurrent_positions} positions at ${r.max_position_notional_usd:.0f} "
            f"exceeds the ${r.max_gross_notional_usd:.0f} gross cap; later positions "
            f"will be shrunk or refused",
        )

    implied_leverage = r.max_gross_notional_usd / r.equity_usd if r.equity_usd > 0 else 0
    if implied_leverage > r.max_leverage:
        rep.add(
            "block", "leverage_inconsistent",
            f"gross cap implies {implied_leverage:.1f}x leverage but max_leverage "
            f"is {r.max_leverage}x",
        )

    # ---- do the kill switches correspond to real money? -------------------

    daily_loss_usd = r.equity_usd * r.max_daily_loss_pct / 100.0
    trades_to_trip = daily_loss_usd / risk_usd if risk_usd > 0 else 0
    if trades_to_trip < 3:
        rep.add(
            "warn", "daily_stop_too_tight",
            f"the daily loss limit (${daily_loss_usd:.2f}) is only {trades_to_trip:.1f} "
            f"full-risk trades. Normal variance will trip it constantly.",
        )
    else:
        rep.add(
            "ok", "daily_stop",
            f"daily loss limit is ${daily_loss_usd:.2f}, about {trades_to_trip:.0f} "
            f"full-risk trades",
        )

    # ---- is the edge requirement survivable given the costs? --------------

    round_trip_bps = (
        cfg.costs.maker_fee_bps + cfg.costs.taker_fee_bps
        + 2 * cfg.costs.residual_slippage_bps
    )
    required_bps = round_trip_bps + r.min_edge_after_costs_bps
    rep.add(
        "ok" if required_bps < 25 else "warn",
        "cost_hurdle",
        f"a signal must predict at least {required_bps:.1f}bps of move to pass "
        f"(round trip {round_trip_bps:.1f}bps + {r.min_edge_after_costs_bps}bps margin). "
        f"On a ${r.max_position_notional_usd:.0f} position that is "
        f"${required_bps / 1e4 * r.max_position_notional_usd:.3f} of gross move needed "
        f"just to break even.",
    )

    # ---- account boundary --------------------------------------------------

    if cfg.account.subaccount_address:
        rep.add("ok", "account_boundary", "trading a subaccount: exposure is bounded by the account")
    else:
        rep.add(
            "warn", "account_boundary",
            "no subaccount: the only bound on exposure is this config. Keep only "
            "your risk capital in the HL account and the rest off-exchange.",
        )

    if cfg.account.require_agent_wallet:
        rep.add("ok", "key_scope", "agent wallet required: the signing key cannot withdraw")
    else:
        rep.add(
            "warn", "key_scope",
            "require_agent_wallet is false: a withdrawal-capable key may be in use",
        )

    # ---- statistical honesty ----------------------------------------------

    rep.add(
        "warn", "sample_size",
        f"at ${risk_usd:.2f} risk per trade, this account size cannot distinguish edge "
        f"from noise — roughly 100+ trades are needed for that, and the variance of "
        f"that sample will dwarf the mean. Treat this phase as a test of the plumbing, "
        f"not of the strategy.",
    )

    return rep
