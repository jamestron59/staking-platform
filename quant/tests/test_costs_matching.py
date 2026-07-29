"""Costs and fill simulation — where optimistic backtests are born."""

from __future__ import annotations

import pytest

from hlq.costs import CostModel, SlippageCalibrator
from hlq.data.book import slippage_bps, walk_book
from hlq.sim.matching import MatchingEngine
from hlq.types import Fill, Order, OrderStatus, Side, TimeInForce, Trade

from .conftest import make_book


@pytest.fixture
def costs() -> CostModel:
    return CostModel(taker_fee_bps=4.5, maker_fee_bps=1.5, residual_slippage_bps=1.0)


def test_walking_the_book_costs_more_than_the_touch(book):
    small_px, _, _ = walk_book(book, Side.BUY, 1.0)
    large_px, _, _ = walk_book(book, Side.BUY, 40.0)
    assert large_px > small_px, "sweeping deeper must produce a worse average price"


def test_size_the_book_cannot_absorb_is_a_veto_not_a_price(book):
    assert slippage_bps(book, Side.BUY, 1e9) is None


def test_round_trip_cost_exceeds_a_typical_small_edge(book, costs):
    """The arithmetic the original design left out: a taker round trip costs
    ~9bps in fees alone, which is more than most 5-minute directional edges."""
    breakdown = costs.estimate_round_trip(book, Side.BUY, 0.01, maker_entry=False)
    assert breakdown is not None
    assert breakdown.entry_fee_bps + breakdown.exit_fee_bps == pytest.approx(9.0)
    assert breakdown.total_bps > 10.0


def test_maker_entry_is_materially_cheaper(book, costs):
    taker = costs.estimate_round_trip(book, Side.BUY, 0.01, maker_entry=False)
    maker = costs.estimate_round_trip(book, Side.BUY, 0.01, maker_entry=True)
    assert maker.total_bps < taker.total_bps


def test_shorts_receive_positive_funding(costs):
    """Funding is a cost for longs and a credit for shorts. On HL it is
    frequently larger than the directional edge being chased."""
    long_cost = costs.funding_bps(1e-4, hold_hours=8, side=Side.BUY)
    short_cost = costs.funding_bps(1e-4, hold_hours=8, side=Side.SELL)
    assert long_cost > 0 and short_cost < 0
    assert long_cost == pytest.approx(-short_cost)


def test_post_only_that_would_cross_is_rejected_not_repriced(book):
    """Modelling it as a taker fill would invent fills that never happened."""
    engine = MatchingEngine(taker_fee_bps=4.5, maker_fee_bps=1.5)
    crossing = Order(cloid="c1", coin="BTC", side=Side.BUY, sz=0.01,
                     limit_px=book.best_ask + 10, tif=TimeInForce.ALO)
    fill, rejection = engine.submit(crossing, book, 1000)
    assert fill is None
    assert rejection == "post_only_would_cross"
    assert crossing.status is OrderStatus.REJECTED


def test_resting_order_waits_behind_the_queue(book):
    """We join the back of the queue. Size ahead of us must trade first."""
    engine = MatchingEngine(taker_fee_bps=4.5, maker_fee_bps=1.5, latency_ms=0)
    px = book.best_bid
    queue_ahead = next(l.sz for l in book.bids if l.px == px)

    order = Order(cloid="c2", coin="BTC", side=Side.BUY, sz=1.0,
                  limit_px=px, tif=TimeInForce.ALO)
    engine.submit(order, book, 1000)
    assert order.status is OrderStatus.OPEN

    # A sell print smaller than the queue ahead must not fill us; it only
    # advances our place in line.
    partial = Trade(coin="BTC", exchange_ms=1100, px=px, sz=queue_ahead * 0.5,
                    aggressor=Side.SELL)
    assert engine.on_trade(partial, 1100) == []
    assert order.filled_sz == 0.0

    # The next print must first consume the REMAINING half of the queue; only
    # the excess reaches us.
    clearing = Trade(coin="BTC", exchange_ms=1200, px=px,
                     sz=queue_ahead * 0.5 + 0.3, aggressor=Side.SELL)
    fills = engine.on_trade(clearing, 1200)
    assert fills and fills[0].sz == pytest.approx(0.3)
    assert not fills[0].crossed  # maker fill: no spread paid
    assert order.status is OrderStatus.PARTIAL


def test_same_side_aggressor_never_fills_a_resting_order(book):
    """Our resting bid is hit by sellers, not by buyers."""
    engine = MatchingEngine(taker_fee_bps=4.5, maker_fee_bps=1.5, latency_ms=0)
    order = Order(cloid="c3", coin="BTC", side=Side.BUY, sz=1.0,
                  limit_px=book.best_bid, tif=TimeInForce.ALO)
    engine.submit(order, book, 1000)
    buy_print = Trade(coin="BTC", exchange_ms=1100, px=book.best_bid, sz=999.0,
                      aggressor=Side.BUY)
    assert engine.on_trade(buy_print, 1100) == []


def test_latency_delays_when_an_order_becomes_live(book):
    engine = MatchingEngine(taker_fee_bps=4.5, maker_fee_bps=1.5, latency_ms=500)
    order = Order(cloid="c4", coin="BTC", side=Side.BUY, sz=1.0,
                  limit_px=book.best_bid, tif=TimeInForce.ALO)
    engine.submit(order, book, 1000)
    through = Trade(coin="BTC", exchange_ms=1100, px=book.best_bid - 100,
                    aggressor=Side.SELL, sz=50.0)
    assert engine.on_trade(through, 1100) == [], "filled before the order was live"
    assert engine.on_trade(through, 1600) != []


def test_ioc_respects_its_limit_price(book):
    engine = MatchingEngine(taker_fee_bps=4.5, maker_fee_bps=1.5)
    order = Order(cloid="c5", coin="BTC", side=Side.BUY, sz=1000.0,
                  limit_px=book.best_ask, tif=TimeInForce.IOC)
    fill, _ = engine.submit(order, book, 1000)
    if fill:
        assert fill.px <= book.best_ask + 1e-9


def test_slippage_calibrator_measures_the_gap_between_decision_and_fill():
    cal = SlippageCalibrator()
    for _ in range(60):
        cal.observe(100.0, Fill(coin="BTC", exchange_ms=0, px=100.02, sz=1.0,
                                side=Side.BUY, fee=0.0, oid=1, tid=1, crossed=True))
    assert cal.suggested_residual_bps() == pytest.approx(2.0, abs=0.01)
    assert cal.summary()["n"] == 60
