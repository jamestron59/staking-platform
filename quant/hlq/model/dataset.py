"""Dataset construction: replay recordings through the production pipeline.

The critical property: training vectors are produced by the same
`FeaturePipeline` object the live loop uses, fed the same events in the same
arrival order. There is no separate research implementation to drift from
production, which is the mechanism by which train/serve skew normally enters.

Labels are attached afterwards, from a price series built from the same replay.
Because `triple_barrier` looks forward from each sample, and features look only
backward, the two never touch — and `t1` is carried through so the validation
code can purge overlapping windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..data.replay import Event, replay
from ..features.pipeline import FeaturePipeline
from ..logging_setup import get_logger
from ..types import BookSnapshot, PerpContext, Trade
from .labels import LabelledSample, PricePoint, triple_barrier

log = get_logger(__name__)


@dataclass
class Dataset:
    feature_names: list[str] = field(default_factory=list)
    X: list[list[float]] = field(default_factory=list)
    y: list[int] = field(default_factory=list)
    t0: list[int] = field(default_factory=list)
    t1: list[int] = field(default_factory=list)
    net_bps: list[float] = field(default_factory=list)
    coins: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.X)

    def summary(self) -> dict:
        if not self.X:
            return {"n": 0}
        return {
            "n": len(self.X),
            "n_features": len(self.feature_names),
            "positive_rate": round(sum(self.y) / len(self.y), 4),
            "mean_net_bps": round(sum(self.net_bps) / len(self.net_bps), 3),
            "span_hours": round((max(self.t1) - min(self.t0)) / 3_600_000, 1),
            "coins": sorted(set(self.coins)),
        }


def build_dataset(
    root: str,
    coins: list[str],
    *,
    sample_interval_ms: int = 60_000,
    horizon_ms: int = 300_000,
    profit_mult: float = 1.5,
    stop_mult: float = 1.0,
    round_trip_cost_bps: float = 12.0,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dataset:
    """Two passes: collect (features, price series), then label.

    Two passes rather than one because labelling needs the future, and the only
    safe way to give it the future is to finish collecting the present first.
    """
    pipeline = FeaturePipeline(coins)
    ds = Dataset(feature_names=pipeline.feature_names)

    prices: dict[str, list[PricePoint]] = {c: [] for c in coins}
    pending: list[tuple[str, int, dict[str, float], float, int]] = []
    last_sample: dict[str, int] = {}

    for ev in replay(root, start_ms=start_ms, end_ms=end_ms):
        pipeline.dispatch(ev)
        payload = ev.payload
        if not isinstance(payload, BookSnapshot):
            continue
        coin, mid = payload.coin, payload.mid
        if coin not in prices or mid is None:
            continue
        prices[coin].append(PricePoint(ev.local_ms, mid))

        if ev.local_ms - last_sample.get(coin, 0) < sample_interval_ms:
            continue
        fv = pipeline.snapshot(coin, ev.local_ms)
        if fv is None or not fv.complete:
            continue
        last_sample[coin] = ev.local_ms
        vol = fv.values.get("realised_vol", 0.0)
        pending.append((coin, ev.local_ms, dict(fv.values), vol, len(prices[coin]) - 1))

    log.event("dataset_pass1_complete", candidates=len(pending),
              price_points={c: len(p) for c, p in prices.items()})

    dropped = {"no_vol": 0, "target_below_cost": 0, "no_future": 0}
    for coin, ts, values, vol, idx in pending:
        if vol <= 0:
            dropped["no_vol"] += 1
            continue
        for side in (1, -1):
            sample = triple_barrier(
                prices[coin], start_index=idx, side=side, volatility=vol,
                profit_mult=profit_mult, stop_mult=stop_mult,
                horizon_ms=horizon_ms, round_trip_cost_bps=round_trip_cost_bps,
            )
            if sample is None:
                dropped["target_below_cost"] += 1
                continue
            ds.X.append([values.get(n, 0.0) for n in ds.feature_names] + [float(side)])
            ds.y.append(1 if sample.net_ret_bps > 0 else 0)
            ds.t0.append(sample.t0_ms)
            ds.t1.append(sample.t1_ms)
            ds.net_bps.append(sample.net_ret_bps)
            ds.coins.append(coin)

    if ds.X:
        ds.feature_names = ds.feature_names + ["side"]
    log.event("dataset_built", dropped=dropped, **ds.summary())
    return ds
