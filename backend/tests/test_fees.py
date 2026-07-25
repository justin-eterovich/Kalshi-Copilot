"""Unit tests for the fee module.

Locks down the details that are easiest to get wrong and most expensive to
get wrong: aggregate (not per-contract) rounding, fail-closed behaviour on
unverified categories, and correct handling of the API's real units —
dollar-denominated prices and fractional contracts.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.core.fees import (
    FeeSchedule,
    UnverifiedFeeCategory,
    maker_fee_cents,
    net_edge_cents,
    round_trip_cost_cents,
    taker_fee_cents,
)


@pytest.fixture
def schedule() -> FeeSchedule:
    """A fully verified schedule: standard rate, crypto at 2x, sports maker-free."""
    return FeeSchedule.from_dict(
        {
            "meta": {"verified_on": "2026-07-25", "schedule_revision": "test"},
            "formula": {"base_taker_rate": 0.07, "maker_rate_fraction": 0.25},
            "categories": {"default": 1.0, "crypto": 2.0, "sports": 1.0},
            "maker_free_categories": ["sports"],
        }
    )


@pytest.fixture
def unverified_schedule() -> FeeSchedule:
    """Mirrors the shipped default: crypto multiplier not yet confirmed."""
    return FeeSchedule.from_dict(
        {
            "meta": {"verified_on": None},
            "formula": {"base_taker_rate": 0.07, "maker_rate_fraction": 0.25},
            "categories": {"default": 1.0, "crypto": None},
        }
    )


def dollars(cents: int | float) -> str:
    """Helper: express a cent price the way the API sends it."""
    return f"{cents / 100:.4f}"


# ---------------------------------------------------------------------------
# Aggregate rounding — the headline correctness property
# ---------------------------------------------------------------------------


def test_single_contract_at_fifty_cents_rounds_up(schedule: FeeSchedule) -> None:
    # 0.07 * 1 * 0.5 * 0.5 = $0.0175 -> rounds UP to 2 cents
    assert taker_fee_cents("0.5000", 1, schedule=schedule) == 2


def test_hundred_contracts_at_fifty_cents_is_exact(schedule: FeeSchedule) -> None:
    # 0.07 * 100 * 0.5 * 0.5 = $1.75 exactly -> 175 cents, no rounding up
    assert taker_fee_cents("0.5000", 100, schedule=schedule) == 175


def test_rounding_is_on_aggregate_not_per_contract(schedule: FeeSchedule) -> None:
    """The bug this guards: charging ceil() per contract inflates fees ~14%."""
    per_contract_rounding = 100 * taker_fee_cents("0.5000", 1, schedule=schedule)
    aggregate = taker_fee_cents("0.5000", 100, schedule=schedule)
    assert per_contract_rounding == 200
    assert aggregate == 175
    assert aggregate < per_contract_rounding


@pytest.mark.parametrize(
    ("price_cents", "contracts", "expected"),
    [
        (50, 1, 2),  # $0.0175 -> 2
        (50, 100, 175),  # exact
        (1, 1, 1),  # $0.000693 -> 1 (minimum charge is a cent)
        (1, 1000, 70),  # $0.693 -> $0.70
        (99, 1000, 70),  # symmetric with 1c
        (25, 100, 132),  # 0.07*100*0.25*0.75 = $1.3125 -> 132
        (75, 100, 132),  # symmetric with 25c
        (10, 500, 315),  # 0.07*500*0.10*0.90 = $3.15 exactly
    ],
)
def test_taker_fee_table(
    price_cents: int, contracts: int, expected: int, schedule: FeeSchedule
) -> None:
    assert taker_fee_cents(dollars(price_cents), contracts, schedule=schedule) == expected


def test_fee_is_symmetric_around_fifty_cents(schedule: FeeSchedule) -> None:
    """P*(1-P) is symmetric, so price P and (1-P) cost the same."""
    for price_cents in range(1, 50):
        assert taker_fee_cents(
            dollars(price_cents), 250, schedule=schedule
        ) == taker_fee_cents(dollars(100 - price_cents), 250, schedule=schedule)


def test_fee_peaks_at_fifty_cents(schedule: FeeSchedule) -> None:
    """The fee curve is parabolic and maxes at the money.

    Rounding makes 49c/50c/51c tie at some sizes, so assert that 50c attains
    the maximum rather than that it is the unique argmax.
    """
    fees = {
        p: taker_fee_cents(dollars(p), 1000, schedule=schedule) for p in range(1, 100)
    }
    assert fees[50] == max(fees.values())
    # ...and it strictly beats the wings
    assert fees[50] > fees[25]
    assert fees[50] > fees[75]
    assert fees[50] > fees[5]


def test_fee_matches_closed_form(schedule: FeeSchedule) -> None:
    """Cross-check the Decimal path against plain float math."""
    for price_cents in (3, 17, 42, 50, 68, 91):
        for contracts in (1, 7, 40, 333, 2500):
            p = price_cents / 100
            expected = math.ceil(0.07 * contracts * p * (1 - p) * 100 - 1e-9)
            assert (
                taker_fee_cents(dollars(price_cents), contracts, schedule=schedule)
                == expected
            )


# ---------------------------------------------------------------------------
# Real API units: dollar prices with sub-cent precision, fractional contracts
# ---------------------------------------------------------------------------


def test_accepts_wire_format_dollar_strings(schedule: FeeSchedule) -> None:
    """The API sends prices as strings like '0.5600' — parse, do not round."""
    assert taker_fee_cents("0.5600", 100, schedule=schedule) == 173


def test_sub_cent_price_is_not_rounded_away(schedule: FeeSchedule) -> None:
    """Tick size varies by market, so 6-decimal prices must survive ingest."""
    fine = taker_fee_cents("0.505000", 1000, schedule=schedule)
    coarse = taker_fee_cents("0.500000", 1000, schedule=schedule)
    # 0.07*1000*0.505*0.495 = $17.49825 -> 1750;  at 0.50 -> exactly 1750
    assert fine == 1750
    assert coarse == 1750
    # ...but a bigger offset does move the fee
    assert taker_fee_cents("0.600000", 1000, schedule=schedule) == 1680


def test_fractional_contracts_are_supported(schedule: FeeSchedule) -> None:
    """Kalshi supports fractional contracts down to 0.01."""
    # 0.07 * 2.50 * 0.5 * 0.5 = $0.04375 -> 5 cents
    assert taker_fee_cents("0.5000", "2.50", schedule=schedule) == 5


def test_fractional_contract_fee_is_monotonic(schedule: FeeSchedule) -> None:
    sizes = ["0.01", "0.50", "1.00", "10.00", "100.00"]
    fees = [taker_fee_cents("0.5000", s, schedule=schedule) for s in sizes]
    assert fees == sorted(fees)


def test_decimal_and_string_inputs_agree(schedule: FeeSchedule) -> None:
    assert taker_fee_cents(Decimal("0.5000"), Decimal("100"), schedule=schedule) == (
        taker_fee_cents("0.5000", "100.00", schedule=schedule)
    )


def test_cents_style_price_is_rejected(schedule: FeeSchedule) -> None:
    """Guard the units migration: 50 means $50, which is not a valid price."""
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        taker_fee_cents(50, 100, schedule=schedule)


# ---------------------------------------------------------------------------
# Maker fees
# ---------------------------------------------------------------------------


def test_maker_fee_is_quarter_of_taker_rate(schedule: FeeSchedule) -> None:
    # 0.0175 * 1000 * 0.5 * 0.5 -> $4.375 -> 438 cents
    assert maker_fee_cents("0.5000", 1000, schedule=schedule) == 438
    assert taker_fee_cents("0.5000", 1000, schedule=schedule) == 1750


def test_maker_free_category_costs_nothing(schedule: FeeSchedule) -> None:
    assert maker_fee_cents("0.5000", 1000, "sports", schedule=schedule) == 0
    # ...but taking liquidity there still costs
    assert taker_fee_cents("0.5000", 1000, "sports", schedule=schedule) == 1750


# ---------------------------------------------------------------------------
# Category multipliers
# ---------------------------------------------------------------------------


def test_category_multiplier_scales_fee(schedule: FeeSchedule) -> None:
    standard = taker_fee_cents("0.5000", 100, "politics", schedule=schedule)
    crypto = taker_fee_cents("0.5000", 100, "crypto", schedule=schedule)
    assert standard == 175
    assert crypto == 350  # 2x multiplier


def test_unknown_category_falls_back_to_default(schedule: FeeSchedule) -> None:
    assert taker_fee_cents("0.5000", 100, "weather", schedule=schedule) == 175


def test_category_lookup_is_case_insensitive(schedule: FeeSchedule) -> None:
    assert taker_fee_cents("0.5000", 100, "CRYPTO", schedule=schedule) == 350
    assert taker_fee_cents("0.5000", 100, "  Crypto  ", schedule=schedule) == 350


# ---------------------------------------------------------------------------
# Fail-closed on unverified categories
# ---------------------------------------------------------------------------


def test_unverified_category_raises(unverified_schedule: FeeSchedule) -> None:
    """We must never silently understate a fee and inflate an edge."""
    with pytest.raises(UnverifiedFeeCategory) as exc:
        taker_fee_cents("0.5000", 100, "crypto", schedule=unverified_schedule)
    assert exc.value.category == "crypto"
    assert "refresh_fee_schedule" in str(exc.value)


def test_unverified_schedule_still_prices_default_categories(
    unverified_schedule: FeeSchedule,
) -> None:
    assert (
        taker_fee_cents("0.5000", 100, "politics", schedule=unverified_schedule) == 175
    )


def test_is_verified_flag(
    schedule: FeeSchedule, unverified_schedule: FeeSchedule
) -> None:
    assert schedule.is_verified is True
    assert unverified_schedule.is_verified is False


def test_default_multiplier_may_not_be_null() -> None:
    with pytest.raises(ValueError, match="cannot be null"):
        FeeSchedule.from_dict({"formula": {}, "categories": {"default": None}})


def test_schedule_requires_default_category() -> None:
    with pytest.raises(ValueError, match="must define `default`"):
        FeeSchedule.from_dict({"formula": {}, "categories": {"crypto": 1.0}})


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_price", ["0", "1", "-0.5", "1.5", "2"])
def test_price_must_be_inside_the_book(bad_price: str, schedule: FeeSchedule) -> None:
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        taker_fee_cents(bad_price, 10, schedule=schedule)


def test_unparseable_price_raises(schedule: FeeSchedule) -> None:
    """A malformed quote must never silently read as zero."""
    with pytest.raises(ValueError):
        taker_fee_cents("not-a-price", 10, schedule=schedule)


def test_negative_contracts_rejected(schedule: FeeSchedule) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        taker_fee_cents("0.5000", -1, schedule=schedule)


def test_zero_contracts_is_free(schedule: FeeSchedule) -> None:
    assert taker_fee_cents("0.5000", 0, schedule=schedule) == 0
    assert maker_fee_cents("0.5000", 0, schedule=schedule) == 0


# ---------------------------------------------------------------------------
# Round-trip cost
# ---------------------------------------------------------------------------


def test_round_trip_charges_both_legs(schedule: FeeSchedule) -> None:
    entry = taker_fee_cents("0.4000", 100, schedule=schedule)
    exit_ = taker_fee_cents("0.6000", 100, schedule=schedule)
    assert (
        round_trip_cost_cents(
            "0.4000", 100, exit_price_dollars="0.6000", schedule=schedule
        )
        == entry + exit_
    )


def test_holding_to_settlement_pays_no_exit_fee(schedule: FeeSchedule) -> None:
    """Set-arb and the resolution sniper rely on this."""
    assert round_trip_cost_cents("0.4000", 100, schedule=schedule) == taker_fee_cents(
        "0.4000", 100, schedule=schedule
    )


def test_resting_entry_is_cheaper_than_crossing(schedule: FeeSchedule) -> None:
    taker = round_trip_cost_cents("0.5000", 500, entry_is_taker=True, schedule=schedule)
    maker = round_trip_cost_cents("0.5000", 500, entry_is_taker=False, schedule=schedule)
    assert maker < taker


# ---------------------------------------------------------------------------
# Net edge — the only number the system is allowed to display
# ---------------------------------------------------------------------------


def test_net_edge_subtracts_fees_from_gross(schedule: FeeSchedule) -> None:
    # Fair $0.60, paying $0.55 -> 5c gross. Fee at 0.55/100 = ceil(173.25) = 174c
    # -> 1.74c per contract. Net = 5 - 1.74 = 3.26c
    edge = net_edge_cents("0.6000", "0.5500", 100, schedule=schedule)
    assert edge == Decimal("3.26")


def test_net_edge_includes_slippage(schedule: FeeSchedule) -> None:
    without = net_edge_cents("0.6000", "0.5500", 100, schedule=schedule)
    with_slip = net_edge_cents(
        "0.6000", "0.5500", 100, slippage_cents="0.5", schedule=schedule
    )
    assert with_slip == without - Decimal("0.5")


def test_a_thin_gross_edge_can_be_net_negative(schedule: FeeSchedule) -> None:
    """The whole point of the module: 1c gross at 50c does not pay."""
    assert net_edge_cents("0.5100", "0.5000", 100, schedule=schedule) < 0


def test_net_edge_on_crypto_is_worse_than_standard(schedule: FeeSchedule) -> None:
    standard = net_edge_cents("0.6000", "0.5500", 100, "politics", schedule=schedule)
    crypto = net_edge_cents("0.6000", "0.5500", 100, "crypto", schedule=schedule)
    assert crypto < standard


def test_maker_edge_beats_taker_edge(schedule: FeeSchedule) -> None:
    taker = net_edge_cents("0.6000", "0.5500", 100, is_taker=True, schedule=schedule)
    maker = net_edge_cents("0.6000", "0.5500", 100, is_taker=False, schedule=schedule)
    assert maker > taker


def test_net_edge_of_zero_size_is_zero(schedule: FeeSchedule) -> None:
    assert net_edge_cents("0.6000", "0.5500", 0, schedule=schedule) == Decimal(0)


def test_net_edge_handles_fractional_size(schedule: FeeSchedule) -> None:
    edge = net_edge_cents("0.6000", "0.5500", "2.50", schedule=schedule)
    assert edge < Decimal(5)  # gross is 5c; fees must have eaten some


def test_net_edge_propagates_unverified_category(
    unverified_schedule: FeeSchedule,
) -> None:
    with pytest.raises(UnverifiedFeeCategory):
        net_edge_cents("0.6000", "0.5500", 100, "crypto", schedule=unverified_schedule)
