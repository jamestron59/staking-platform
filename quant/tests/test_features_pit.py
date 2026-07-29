"""The most important test in the repository.

If a feature can see the future, every downstream number is fiction and no
other test can detect it. The backtest still runs, the Sharpe still looks
good, and the strategy still loses money live.

The property tested is simple and total: **truncating the future must not
change the past.** Compute the feature vector at event k during a full replay,
then replay only the first k events and compute it again. If any feature
peeked forward — a centred window, a label leak, an interpolation across the
current timestamp — the two vectors differ.

This catches lookahead that code review does not, because it makes no
assumption about how the leak was introduced.
"""

from __future__ import annotations

import pytest

from hlq.features.pipeline import FeaturePipeline
from hlq.types import BookSnapshot

from .conftest import synthetic_events


def test_truncating_the_future_does_not_change_the_past():
    events = synthetic_events(n=1200)

    full = FeaturePipeline(["BTC"])
    snapshots: list[tuple[int, dict]] = []
    for ev in events:
        full.dispatch(ev)
        snap = full.snapshot("BTC", ev.local_ms)
        snapshots.append((ev.local_ms, dict(snap.values)))

    # Check several cut points, including deep into the series where slow
    # features (10-minute EWMAs, 15-minute OI windows) are fully warmed.
    for k in (200, 600, 1000, len(events) - 1):
        prefix = FeaturePipeline(["BTC"])
        for ev in events[: k + 1]:
            prefix.dispatch(ev)
        got = prefix.snapshot("BTC", events[k].local_ms)
        expected_ts, expected_values = snapshots[k]

        assert got.ts_ms == expected_ts
        assert set(got.values) == set(expected_values), (
            f"feature set differs at k={k}: "
            f"{set(got.values) ^ set(expected_values)}"
        )
        for name, value in expected_values.items():
            assert got.values[name] == pytest.approx(value, rel=1e-12, abs=1e-15), (
                f"feature {name!r} at k={k} changed when future events were removed: "
                f"{got.values[name]} vs {value} — this feature sees the future"
            )


def test_features_are_not_all_constant():
    """Guards the test above: a pipeline that returns zeros for everything
    would pass the lookahead property trivially."""
    events = synthetic_events(n=1500)
    pipe = FeaturePipeline(["BTC"])
    seen: dict[str, set[float]] = {}
    for ev in events:
        pipe.dispatch(ev)
        snap = pipe.snapshot("BTC", ev.local_ms)
        for k, v in snap.values.items():
            seen.setdefault(k, set()).add(round(v, 10))

    assert seen, "pipeline produced no features at all"
    constant = [k for k, vals in seen.items() if len(vals) < 5]
    assert not constant, f"features never varied, so the lookahead test is vacuous: {constant}"


def test_pipeline_becomes_complete_after_warmup():
    """The slowest feature is the 15-minute open-interest window, so the
    pipeline needs ~15 minutes of data before it will emit anything. That
    delay is intentional — it is also a real operational cost after a restart,
    which is why it is asserted rather than left implicit."""
    events = synthetic_events(n=6000)  # ~25 minutes at 250ms per step
    pipe = FeaturePipeline(["BTC"])
    complete_at = None
    for i, ev in enumerate(events):
        pipe.dispatch(ev)
        if pipe.snapshot("BTC", ev.local_ms).complete:
            complete_at = i
            break
    assert complete_at is not None, (
        f"features never became complete; still missing: {pipe.diagnostics()}"
    )
    warmup_ms = events[complete_at].local_ms - events[0].local_ms
    assert warmup_ms >= 900_000, "completed before the OI window could possibly be warm"
    assert warmup_ms <= 1_200_000, f"warmup took {warmup_ms/60000:.1f} min, longer than expected"


def test_gap_resets_derived_state():
    """A feed gap must reset state rather than compute across the hole.

    CVD carried across a four-minute gap returns a plausible number that is
    wrong, and nothing downstream can tell.
    """
    events = synthetic_events(n=7000)
    pipe = FeaturePipeline(["BTC"])
    for ev in events[:6000]:
        pipe.dispatch(ev)
    before = pipe.snapshot("BTC", events[5999].local_ms)
    assert before.complete, f"never warmed up: {pipe.diagnostics()}"

    gap_event = events[6000]
    payload = gap_event.payload
    assert isinstance(payload, BookSnapshot)
    pipe.on_book(payload, after_gap=True)

    after = pipe.snapshot("BTC", payload.exchange_ms)
    assert not after.complete, "pipeline still reports complete immediately after a gap"


def test_no_data_no_features():
    pipe = FeaturePipeline(["BTC"])
    snap = pipe.snapshot("BTC", 0)
    assert snap is not None
    assert not snap.complete
    assert snap.values == {}


def test_unknown_coin_is_ignored_not_invented():
    pipe = FeaturePipeline(["BTC"])
    assert pipe.snapshot("DOGE") is None
