"""Validation that does not lie.

Standard K-fold cross-validation is invalid on this data, for two reasons that
compound:

**Label overlap.** A sample labelled at t0 resolves at t1, often minutes later.
A neighbouring sample at t0+10s has a label built from almost the same future
prices. Put one in train and the other in test and the model has effectively
seen the test answer. *Purging* removes training samples whose label window
overlaps the test window.

**Serial correlation.** Even after purging, samples immediately after the test
window carry information about it through slow-moving state (volatility regime,
open interest). *Embargo* drops a fixed fraction of samples after each test fold.

Both are from López de Prado, and both routinely cut an apparent Sharpe of 2+
down to something near zero — which is the point. The number that survives is
the one worth acting on.

`walk_forward` is the second discipline: train on the past, test on the future
that follows it, roll forward, never let a later period influence an earlier
model. It is slower and produces worse numbers than optimising over the whole
history, and it is the only protocol whose results resemble live trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Sequence


@dataclass(frozen=True, slots=True)
class Split:
    train_idx: list[int]
    test_idx: list[int]
    purged: int
    embargoed: int
    label: str = ""


class PurgedKFold:
    """K-fold with purging and embargo, for samples with resolution times.

    `t0` and `t1` are parallel arrays: sample i is observed at t0[i] and its
    label resolves at t1[i].
    """

    def __init__(self, n_splits: int = 6, embargo_pct: float = 0.01) -> None:
        if n_splits < 2:
            raise ValueError("n_splits must be >= 2")
        if not 0 <= embargo_pct < 0.5:
            raise ValueError("embargo_pct must be in [0, 0.5)")
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct

    def split(self, t0: Sequence[int], t1: Sequence[int]) -> Iterator[Split]:
        if len(t0) != len(t1):
            raise ValueError("t0 and t1 must be the same length")
        n = len(t0)
        if n < self.n_splits * 2:
            raise ValueError(f"too few samples ({n}) for {self.n_splits} splits")

        indices = list(range(n))
        fold_size = n // self.n_splits
        embargo = int(n * self.embargo_pct)

        for k in range(self.n_splits):
            start = k * fold_size
            stop = n if k == self.n_splits - 1 else (k + 1) * fold_size
            test_idx = indices[start:stop]
            test_t0, test_t1 = t0[start], max(t1[start:stop])

            train_idx, purged, embargoed = [], 0, 0
            for i in indices:
                if start <= i < stop:
                    continue
                # Purge: this sample's label window overlaps the test window.
                if t1[i] >= test_t0 and t0[i] <= test_t1:
                    purged += 1
                    continue
                # Embargo: drop samples just after the test fold.
                if stop <= i < min(n, stop + embargo):
                    embargoed += 1
                    continue
                train_idx.append(i)

            yield Split(train_idx, test_idx, purged, embargoed, label=f"fold_{k+1}")


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    label: str

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "train": [self.train_start, self.train_end],
            "test": [self.test_start, self.test_end],
        }


def walk_forward(
    n: int,
    *,
    train_size: int,
    test_size: int,
    step: Optional[int] = None,
    embargo: int = 0,
    anchored: bool = False,
) -> list[WalkForwardWindow]:
    """Rolling (or anchored) train/test windows in chronological order.

    `anchored=True` grows the training window from a fixed start instead of
    sliding it. Anchored uses more data; rolling adapts faster to regime change
    and is the safer default for a system that will be retrained periodically.
    """
    step = step or test_size
    windows: list[WalkForwardWindow] = []
    start = 0
    i = 0
    while start + train_size + embargo + test_size <= n:
        train_start = 0 if anchored else start
        train_end = start + train_size
        test_start = train_end + embargo
        test_end = test_start + test_size
        i += 1
        windows.append(WalkForwardWindow(train_start, train_end, test_start, test_end, f"wf_{i}"))
        start += step
    return windows


def split_report(splits: Sequence[Split]) -> dict:
    """Report how much data purging removed.

    If purging removes most of the training set, the labels overlap too much
    for the sampling frequency — sample less often rather than reducing the
    horizon, which would only make the labels smaller than costs.
    """
    if not splits:
        return {}
    total_train = sum(len(s.train_idx) for s in splits)
    total_purged = sum(s.purged for s in splits)
    total_embargo = sum(s.embargoed for s in splits)
    denom = total_train + total_purged + total_embargo
    return {
        "n_splits": len(splits),
        "avg_train_size": total_train // len(splits),
        "avg_test_size": sum(len(s.test_idx) for s in splits) // len(splits),
        "total_purged": total_purged,
        "total_embargoed": total_embargo,
        "purge_share": round(total_purged / denom, 3) if denom else 0.0,
        "warning": (
            "purging removed over 40% of training data: samples overlap too much, "
            "reduce sampling frequency"
        ) if denom and total_purged / denom > 0.4 else "",
    }
