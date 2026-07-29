"""Shared synthetic market data.

Generated deterministically so tests are reproducible, but with realistic
structure: a random walk with drift, a two-sided book that widens with
volatility, and trades whose aggressor side correlates with price movement.
"""

from __future__ import annotations

import math
import random

import pytest

from hlq.data.replay import Event
from hlq.instruments import Instrument, InstrumentRegistry
from hlq.types import BookSnapshot, Level, PerpContext, Side, Trade


def make_book(
    coin: str, ts: int, mid: float, spread: float = 1.0, depth: float = 5.0,
    skew: float = 1.0, rng: random.Random | None = None,
) -> BookSnapshot:
    """`skew` > 1 means more size resting on the bid than the ask.

    Two kinds of realism matter here, and both were learned by watching tests
    fail for the wrong reason:

      - A perfectly symmetric book makes book_imbalance and microprice
        identically zero.
      - A book whose level sizes follow one smooth profile makes any *ratio*
        of depth bands algebraically constant, because the common factor
        cancels. Real books are lumpy, so level sizes get per-level noise.
    """
    # Levels are spaced so that 12 of them span roughly 30bps, which is the
    # order of magnitude a real HL book covers. Packing them all inside 1bps
    # would make the depth-band features degenerate.
    step = max(spread, mid * 0.00025)

    def sz(i: int, side_skew: float) -> float:
        base = depth * side_skew * (1 + i * 0.4)
        return base * (math.exp(rng.gauss(0, 0.5)) if rng else 1.0)

    bids = tuple(Level(round(mid - spread / 2 - i * step, 4), sz(i, skew), 3)
                 for i in range(12))
    asks = tuple(Level(round(mid + spread / 2 + i * step, 4), sz(i, 1 / skew), 3)
                 for i in range(12))
    return BookSnapshot(coin=coin, exchange_ms=ts, bids=bids, asks=asks, local_ms=ts)


def synthetic_events(
    n: int = 4000, coin: str = "BTC", start_px: float = 50_000.0, seed: int = 7
) -> list[Event]:
    """Interleaved book / trade / context events on a 250ms grid."""
    rng = random.Random(seed)
    events: list[Event] = []
    px = start_px
    ts = 1_700_000_000_000
    # Order flow in real markets is persistent: buyers arrive in clusters, not
    # independently. An iid aggressor side makes any windowed flow measure
    # concentrate at zero, so a flow-based strategy would never fire and the
    # end-to-end tests would pass vacuously.
    #
    # The decay constant matters more than it looks. At 250ms per step, 0.997
    # gives a half-life of about a minute, so flow episodes survive the
    # 5-minute measurement window. A faster decay (0.94 => ~3s half-life)
    # averages out to nothing inside that window, which is how the first
    # version of this fixture produced zero signals.
    flow_bias = 0.0
    for i in range(n):
        ts += 250
        flow_bias = 0.997 * flow_bias + rng.gauss(0, 0.08)
        # Flow pushes price a little, as it does in reality.
        px *= math.exp(rng.gauss(0, 0.0004) + 0.00002 * math.tanh(flow_bias))
        book = make_book(
            coin, ts, round(px, 1),
            spread=max(0.5, abs(rng.gauss(1.0, 0.3))),
            skew=math.exp(rng.gauss(0, 0.25)),
            rng=rng,
        )
        events.append(Event(ts, "l2Book", book))
        if i % 2 == 0:
            p_buy = 0.5 + 0.45 * math.tanh(flow_bias)
            side = Side.BUY if rng.random() < p_buy else Side.SELL
            events.append(Event(ts + 10, "trades", Trade(
                coin=coin, exchange_ms=ts + 10, px=round(px, 1),
                sz=abs(rng.gauss(0.5, 0.2)) + 0.01, aggressor=side, tid=i, local_ms=ts + 10,
            )))
        if i % 40 == 0:
            events.append(Event(ts + 20, "activeAssetCtx", PerpContext(
                coin=coin, exchange_ms=ts + 20, mark_px=round(px, 1),
                oracle_px=round(px * (1 + rng.gauss(0, 0.0001)), 2),
                funding=1.25e-5 + rng.gauss(0, 3e-6), open_interest=1000 + i * 0.5,
                premium=0.0001, local_ms=ts + 20,
            )))
    return events


@pytest.fixture
def btc() -> Instrument:
    return Instrument(name="BTC", asset_id=0, sz_decimals=5, max_leverage=40)


@pytest.fixture
def eth() -> Instrument:
    return Instrument(name="ETH", asset_id=1, sz_decimals=4, max_leverage=25)


@pytest.fixture
def registry(btc, eth) -> InstrumentRegistry:
    return InstrumentRegistry([btc, eth])


@pytest.fixture
def events() -> list[Event]:
    return synthetic_events()


@pytest.fixture
def book() -> BookSnapshot:
    return make_book("BTC", 1_700_000_000_000, 50_000.0, spread=1.0, depth=5.0)
