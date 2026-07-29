"""Tick and lot arithmetic. A wrong price here is a rejected order at best and
a wrong fill at worst."""

from __future__ import annotations

import pytest

from hlq.instruments import Instrument, InstrumentRegistry, Rounding
from hlq.types import Side


def sdk_rule(px: float, sz_decimals: int, is_spot: bool = False) -> float:
    """Exactly what the official SDK's `Exchange._slippage_price` computes."""
    return round(float(f"{px:.5g}"), (6 if not is_spot else 8) - sz_decimals)


@pytest.mark.parametrize("sz_decimals,prices", [
    (5, [118432.7, 3.14159, 99999.99, 1.23456, 12345.678]),
    (4, [3456.789, 1.234567, 0.5555555]),
    (2, [187.6543, 1234.5678]),
    (0, [0.0123456789, 1.23456789]),
])
def test_strict_mode_matches_sdk_exactly(sz_decimals, prices):
    inst = Instrument(name="X", asset_id=0, sz_decimals=sz_decimals, strict_sig_figs=True)
    for px in prices:
        assert inst.round_price(px, Rounding.NEAREST) == pytest.approx(
            sdk_rule(px, sz_decimals), abs=1e-9
        )


def test_integer_exemption_gives_a_finer_tick_than_the_sdk(btc):
    """HL accepts integer prices regardless of significant figures, which is
    what lets BTC quote on a $1 tick. The SDK's slippage helper truncates to 5
    significant figures and would quote a $10 tick."""
    assert btc.round_price(118432.7, Rounding.NEAREST) == 118433.0
    assert sdk_rule(118432.7, 5) == 118430.0
    assert btc.is_valid_price(118433.0)


def test_passive_rounding_never_crosses_the_spread(eth):
    bid, ask = 3456.7891, 3456.8123
    assert eth.round_price(bid, Rounding.passive_for(Side.BUY)) <= bid
    assert eth.round_price(ask, Rounding.passive_for(Side.SELL)) >= ask


def test_aggressive_rounding_moves_toward_the_market(eth):
    assert eth.round_price(3456.7891, Rounding.aggressive_for(Side.BUY)) >= 3456.7891
    assert eth.round_price(3456.8123, Rounding.aggressive_for(Side.SELL)) <= 3456.8123


@pytest.mark.parametrize("size", [0.123456789, 0.999999, 1.000001, 12.3456789])
def test_size_always_rounds_toward_zero(btc, size):
    """We may take less risk than intended, never more."""
    assert btc.round_size(size) <= size
    assert btc.is_valid_size(btc.round_size(size))


def test_absurd_price_raises_rather_than_fabricating(btc):
    """A price orders of magnitude off implies a unit bug or corrupt feed.
    Substituting a plausible tick would turn a caught bug into a live order."""
    with pytest.raises(ValueError, match="refusing to fabricate"):
        btc.round_price(0.000123456, Rounding.NEAREST)


def test_rounded_prices_are_always_wire_valid(btc, eth):
    for inst in (btc, eth):
        for px in (1.0, 12.3456, 999.99, 45678.9, 118432.7):
            for mode in Rounding:
                assert inst.is_valid_price(inst.round_price(px, mode)), (
                    f"{inst.name} produced an invalid price from {px} via {mode}"
                )


def test_min_notional_is_enforced(btc):
    assert not btc.meets_min_notional(50_000.0, 0.00001)  # $0.50
    assert btc.meets_min_notional(50_000.0, 0.001)  # $50


def test_registry_rejects_unknown_symbols_loudly(registry):
    with pytest.raises(KeyError, match="unknown instrument"):
        registry.get("NOTACOIN")
    assert "BTC" in registry


def test_registry_from_meta_assigns_index_as_asset_id():
    reg = InstrumentRegistry.from_meta({
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        ]
    })
    assert reg.get("BTC").asset_id == 0
    assert reg.get("ETH").asset_id == 1
    assert reg.get("ETH").max_price_decimals == 2
