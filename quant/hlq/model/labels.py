"""Labelling: what the model is actually asked to predict.

The original design had five models each answering a fuzzy question — "is the
market bullish?", "does it have momentum?". Those questions have no objective
answer, so each one needs a hand-made label definition, each definition is an
arbitrary choice, and none of them is "this trade made money after costs".

The triple-barrier method (López de Prado) replaces all of that with one
economically meaningful question. From a candidate entry at t0, place three
barriers:

  - upper: the profit target;
  - lower: the stop;
  - vertical: a time limit.

The label is whichever barrier price touches first. That is exactly the outcome
the trade would have had, including the case where nothing happens and the
position is closed flat at the time limit — which a fixed-horizon return label
misclassifies as a small win or loss.

Two refinements that matter:

**Barriers are volatility-scaled.** A fixed 0.5% target means something
different in a quiet hour than during a cascade. Scaling by realised volatility
makes labels comparable across regimes; without it the model mostly learns to
recognise volatility.

**Barriers are cost-aware.** The upper barrier must clear the round-trip cost,
otherwise the model is rewarded for predicting moves too small to trade — the
most common way a profitable-looking classifier produces a losing system.

`t1` (the touch time) is returned alongside the label because the validation
code needs it: samples whose outcome window overlaps the test set must be
purged from training, and that is impossible to do without knowing when each
label resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Sequence


class Label(IntEnum):
    LOSS = -1
    NEUTRAL = 0
    WIN = 1


@dataclass(frozen=True, slots=True)
class LabelledSample:
    t0_ms: int
    t1_ms: int  # when the outcome resolved — required for purging
    label: Label
    ret: float  # realised return over the holding period, net of nothing
    net_ret_bps: float  # after costs: the number that decides deployment
    barrier_hit: str  # "upper" | "lower" | "vertical"
    side: int  # +1 long, -1 short


@dataclass(frozen=True, slots=True)
class PricePoint:
    ts_ms: int
    px: float


def triple_barrier(
    prices: Sequence[PricePoint],
    *,
    start_index: int,
    side: int,
    volatility: float,
    profit_mult: float,
    stop_mult: float,
    horizon_ms: int,
    round_trip_cost_bps: float,
    min_target_over_cost: float = 1.5,
) -> Optional[LabelledSample]:
    """Label one candidate entry. None when the sample is unusable.

    `min_target_over_cost` enforces that the profit target is a multiple of the
    round-trip cost. If a 1.5x-volatility target does not clear that bar, the
    sample is dropped rather than labelled — training on trades that cannot be
    profitable teaches the model to find them.
    """
    if start_index >= len(prices) - 1 or volatility <= 0 or side not in (1, -1):
        return None

    entry = prices[start_index]
    if entry.px <= 0:
        return None

    target_bps = 1e4 * volatility * profit_mult
    stop_bps = 1e4 * volatility * stop_mult
    if target_bps < round_trip_cost_bps * min_target_over_cost:
        return None

    upper = entry.px * (1 + side * target_bps / 1e4)
    lower = entry.px * (1 - side * stop_bps / 1e4)
    deadline = entry.ts_ms + horizon_ms

    for p in prices[start_index + 1:]:
        if p.ts_ms > deadline:
            break
        hit_upper = (p.px >= upper) if side == 1 else (p.px <= upper)
        hit_lower = (p.px <= lower) if side == 1 else (p.px >= lower)
        # When both barriers fall inside the same observation gap we cannot know
        # which came first. Assume the loss: the pessimistic reading is the only
        # one that does not manufacture edge.
        if hit_lower:
            return _make(entry, p, Label.LOSS, side, "lower", round_trip_cost_bps)
        if hit_upper:
            return _make(entry, p, Label.WIN, side, "upper", round_trip_cost_bps)

    last = next((p for p in reversed(prices) if p.ts_ms <= deadline), None)
    if last is None or last.ts_ms == entry.ts_ms:
        return None
    return _make(entry, last, Label.NEUTRAL, side, "vertical", round_trip_cost_bps)


def _make(
    entry: PricePoint, exit_p: PricePoint, label: Label, side: int,
    barrier: str, cost_bps: float,
) -> LabelledSample:
    ret = side * (exit_p.px - entry.px) / entry.px
    return LabelledSample(
        t0_ms=entry.ts_ms,
        t1_ms=exit_p.ts_ms,
        label=label,
        ret=ret,
        net_ret_bps=1e4 * ret - cost_bps,
        barrier_hit=barrier,
        side=side,
    )


def meta_label(samples: Sequence[LabelledSample]) -> list[int]:
    """Meta-labelling: given that a primary rule fired, should we take the trade?

    This is the honest version of the original "Meta AI". Rather than stacking
    five opaque scores and hoping the combination means something, the primary
    model (or a simple rule) decides *direction*, and a second binary model
    decides *whether to act* — trained on whether the primary's signals actually
    paid after costs.

    The advantage is that it is testable in isolation: a meta-model that cannot
    beat "always take it" is adding nothing, and you find that out immediately
    instead of after it is wired into a confidence score.
    """
    return [1 if s.net_ret_bps > 0 else 0 for s in samples]


def label_distribution(samples: Sequence[LabelledSample]) -> dict[str, float | int]:
    """Sanity check before training. Heavily imbalanced or almost entirely
    vertical-barrier labels means the barriers are mis-scaled, and no amount of
    model tuning fixes a labelling problem."""
    if not samples:
        return {}
    n = len(samples)
    counts = {l.name: sum(1 for s in samples if s.label is l) for l in Label}
    barriers = {b: sum(1 for s in samples if s.barrier_hit == b)
                for b in ("upper", "lower", "vertical")}
    profitable = sum(1 for s in samples if s.net_ret_bps > 0)
    return {
        "n": n,
        **{f"label_{k.lower()}": v for k, v in counts.items()},
        **{f"barrier_{k}": v for k, v in barriers.items()},
        "share_vertical": round(barriers["vertical"] / n, 3),
        "share_profitable_after_costs": round(profitable / n, 3),
        "mean_net_bps": round(sum(s.net_ret_bps for s in samples) / n, 2),
    }
