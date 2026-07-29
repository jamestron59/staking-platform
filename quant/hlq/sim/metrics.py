"""Performance metrics, including the ones that survive honest scrutiny.

A raw Sharpe ratio from a backtest is close to meaningless on its own, because
it does not account for how many strategies you tried before finding it. Test
100 feature/threshold combinations on the same data and the best one will show
a Sharpe near 2 even if none of them has any edge at all.

`deflated_sharpe` implements Bailey & López de Prado's correction: it asks
"given that I ran N trials, and given the skew and kurtosis of these returns,
what is the probability the true Sharpe is above zero?" A strategy with a
backtest Sharpe of 2.5 found after 200 trials routinely deflates to a DSR below
0.5, meaning it is more likely noise than edge. That number is the one to
report — and `n_trials` must be the honest count of everything tried, including
the variants you discarded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

EULER_MASCHERONI = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Accurate to ~1e-9 over (0,1), which is far beyond what the surrounding
    statistics justify, and avoids a scipy import in the hot path.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0,1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _moments(x: Sequence[float]) -> tuple[float, float, float, float]:
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0, 3.0
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd <= 0:
        return mean, 0.0, 0.0, 3.0
    m3 = sum((v - mean) ** 3 for v in x) / n
    m4 = sum((v - mean) ** 4 for v in x) / n
    return mean, sd, m3 / sd**3, m4 / sd**4


def sharpe(returns: Sequence[float], periods_per_year: float) -> float:
    mean, sd, _, _ = _moments(returns)
    if sd <= 0:
        return 0.0
    return mean / sd * math.sqrt(periods_per_year)


def probabilistic_sharpe(returns: Sequence[float], benchmark_sr: float, periods_per_year: float) -> float:
    """P(true Sharpe > benchmark), adjusted for non-normal returns.

    Trading returns are skewed and fat-tailed; ignoring that overstates
    significance, because a few large wins inflate the mean without the
    variance catching up.
    """
    n = len(returns)
    if n < 3:
        return 0.0
    _, _, skew, kurt = _moments(returns)
    sr = sharpe(returns, periods_per_year) / math.sqrt(periods_per_year)  # per-period
    bench = benchmark_sr / math.sqrt(periods_per_year)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return _norm_cdf((sr - bench) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Sharpe you would expect from the *best* of N strategies with zero edge."""
    if n_trials < 2:
        return 0.0
    e = math.e
    return math.sqrt(sr_variance) * (
        (1 - EULER_MASCHERONI) * _norm_ppf(1 - 1.0 / n_trials)
        + EULER_MASCHERONI * _norm_ppf(1 - 1.0 / (n_trials * e))
    )


def deflated_sharpe(
    returns: Sequence[float], n_trials: int, periods_per_year: float, sr_variance: Optional[float] = None
) -> float:
    """DSR: probability the strategy's true Sharpe exceeds what N trials of
    pure noise would have produced. Below ~0.95, do not deploy."""
    if len(returns) < 3 or n_trials < 1:
        return 0.0
    if sr_variance is None:
        # Variance of SR estimates across trials; without the actual spread of
        # your trials, 1/(n-1) is the standard fallback.
        sr_variance = 1.0 / max(1, len(returns) - 1)
    sr0 = expected_max_sharpe(n_trials, sr_variance) * math.sqrt(periods_per_year)
    return probabilistic_sharpe(returns, sr0, periods_per_year)


def max_drawdown(equity: Sequence[float]) -> tuple[float, int, int]:
    """Returns (depth as a fraction, peak index, trough index)."""
    if not equity:
        return 0.0, 0, 0
    peak, peak_i, worst, wi, wj = equity[0], 0, 0.0, 0, 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > worst:
            worst, wi, wj = dd, peak_i, i
    return worst, wi, wj


@dataclass
class TradeRecord:
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
    reason: str = ""

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding

    @property
    def hold_ms(self) -> int:
        return self.exit_ms - self.entry_ms


@dataclass
class BacktestResult:
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[int] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    initial_equity: float = 0.0
    n_trials: int = 1
    rejections: dict[str, int] = field(default_factory=dict)

    def period_returns(self) -> list[float]:
        eq = self.equity_curve
        return [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq)) if eq[i - 1] > 0]

    def summary(self, periods_per_year: float = 365 * 24) -> dict[str, float | int]:
        rets = self.period_returns()
        eq = self.equity_curve or [self.initial_equity]
        dd, _, _ = max_drawdown(eq)
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = -sum(t.net_pnl for t in losses)
        total_fees = sum(t.fees for t in self.trades)
        total_funding = sum(t.funding for t in self.trades)
        gross = sum(t.gross_pnl for t in self.trades)
        net = sum(t.net_pnl for t in self.trades)
        return {
            "n_trades": len(self.trades),
            "net_pnl": round(net, 2),
            "gross_pnl": round(gross, 2),
            "fees_paid": round(total_fees, 2),
            "funding_paid": round(total_funding, 2),
            # If costs eat most of gross, the "edge" is a fee-generation machine.
            "cost_share_of_gross": round(
                (total_fees + total_funding) / abs(gross), 3) if gross else 0.0,
            "return_pct": round(100 * (eq[-1] / self.initial_equity - 1), 3) if self.initial_equity else 0.0,
            "sharpe": round(sharpe(rets, periods_per_year), 3),
            "deflated_sharpe": round(deflated_sharpe(rets, self.n_trials, periods_per_year), 4),
            "n_trials_declared": self.n_trials,
            "max_drawdown_pct": round(100 * dd, 2),
            "win_rate": round(len(wins) / len(self.trades), 3) if self.trades else 0.0,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else 0.0,
            "avg_hold_minutes": round(
                sum(t.hold_ms for t in self.trades) / len(self.trades) / 60000, 2) if self.trades else 0.0,
        }

    def verdict(self, periods_per_year: float = 365 * 24) -> tuple[bool, list[str]]:
        """Deployment gate. Deliberately harsh — the default answer is no."""
        s = self.summary(periods_per_year)
        reasons = []
        if s["n_trades"] < 100:
            reasons.append(f"only {s['n_trades']} trades: not enough to distinguish edge from luck")
        if s["deflated_sharpe"] < 0.95:
            reasons.append(f"deflated Sharpe {s['deflated_sharpe']} < 0.95 after {self.n_trials} trials")
        if s["net_pnl"] <= 0:
            reasons.append("net PnL is not positive after costs")
        if s["cost_share_of_gross"] > 0.6:
            reasons.append(f"costs consume {100*s['cost_share_of_gross']:.0f}% of gross: edge is marginal")
        if s["max_drawdown_pct"] > 25:
            reasons.append(f"max drawdown {s['max_drawdown_pct']}% too deep to size confidently")
        return (not reasons), reasons
