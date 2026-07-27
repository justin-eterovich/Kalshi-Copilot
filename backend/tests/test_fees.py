"""Unit tests for the fee module.

Every edge figure in the system is net of fees, so these are the tests that
decide whether any other number can be trusted.

Three things here were wrong until the schedule was read against the official
PDF and checked against real fills on the demo exchange, and each has a test
named for it:

- **Rounding is to a centicent** (``$0.0001``), not a cent. One contract at
  50c costs 1.75c, not 2c.
- **Maker fees default to zero.** The documented default maker multiplier is
  0, so an unlisted series pays no maker fee at all.
- **Multipliers are keyed by series ticker**, not category. The schedule has
  no category dimension; assuming one excluded ~50,000 markets from proposals
  over a multiplier that does not exist.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

import pytest

from app.core.fees import (
    FeeSchedule,
    UnknownSeries,
    UnverifiedFeeSchedule,
    maker_fee_cents,
    net_edge_cents,
    round_trip_cost_cents,
    series_of,
    taker_fee_cents,
)


@pytest.fixture
def schedule() -> FeeSchedule:
    """Mirrors the shape of the real schedule: defaults plus exceptions."""
    return FeeSchedule.from_dict(
        {
            "meta": {"verified_on": "2026-07-27", "schedule_revision": "test"},
            "formula": {"base_taker_rate": "0.07", "base_maker_rate": "0.0175"},
            "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
            "series": {
                # standard taker, and maker fees DO apply
                "KXCPI": {"maker": 1, "taker": 1},
                # no trading fees at all
                "KXBTCY": {"maker": 0, "taker": 0},
            },
        }
    )


@pytest.fixture
def unverified() -> FeeSchedule:
    return FeeSchedule.from_dict(
        {
            "meta": {"verified_on": None},
            "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
            "series": {},
        }
    )


# ---------------------------------------------------------------------------
# Rounding — the thing that was wrong
# ---------------------------------------------------------------------------


class TestCenticentRounding:
    def test_one_contract_at_fifty_cents_costs_1_75_not_2(
        self, schedule: FeeSchedule
    ) -> None:
        """The schedule rounds to a centicent, so there is nothing to round.

        0.07 * 1 * 0.5 * 0.5 = $0.0175 exactly = 1.75 cents. Rounding up to a
        whole cent — as this engine used to — overstates it by 14%.
        """
        assert taker_fee_cents("0.50", "1", schedule=schedule) == Decimal("1.75")

    def test_matches_what_the_demo_exchange_actually_billed(
        self, schedule: FeeSchedule
    ) -> None:
        """Ground truth, not theory. These are real fills."""
        assert taker_fee_cents("0.20", "2", schedule=schedule) == Decimal("2.24")
        assert taker_fee_cents("0.20", "3", schedule=schedule) == Decimal("3.36")

    def test_rounds_up_when_the_value_is_finer_than_a_centicent(
        self, schedule: FeeSchedule
    ) -> None:
        # 0.07 * 1 * 0.333333 * 0.666667 = $0.01555554... -> $0.0156
        assert taker_fee_cents("0.333333", "1", schedule=schedule) == Decimal("1.56")

    def test_rounding_is_up_never_down(self, schedule: FeeSchedule) -> None:
        for price in ("0.13", "0.27", "0.41", "0.6789"):
            p = Decimal(price)
            exact = Decimal("0.07") * p * (Decimal(1) - p) * Decimal(100)
            assert taker_fee_cents(price, "1", schedule=schedule) >= exact

    def test_rounding_is_on_the_aggregate_not_per_contract(
        self, schedule: FeeSchedule
    ) -> None:
        """100 contracts at 50c cost exactly $1.75, not 100 separate roundings."""
        one = taker_fee_cents("0.50", "1", schedule=schedule)
        hundred = taker_fee_cents("0.50", "100", schedule=schedule)
        assert hundred == Decimal("175")
        assert hundred <= one * 100


class TestAgainstThePublishedTable:
    """The PDF prints a fee table for 100 contracts at each price.

    Its figures are the exact fee rounded **up to the cent** for display —
    $0.3325 prints as $0.34. So the check is that our centicent-exact value
    ceils to the printed one, which validates both our arithmetic and our
    reading of the table.
    """

    @pytest.mark.parametrize(
        ("price", "expected_dollars"),
        [
            ("0.01", "0.07"), ("0.05", "0.34"), ("0.10", "0.63"),
            ("0.15", "0.90"), ("0.20", "1.12"), ("0.25", "1.32"),
            ("0.30", "1.47"), ("0.35", "1.60"), ("0.40", "1.68"),
            ("0.45", "1.74"), ("0.50", "1.75"), ("0.55", "1.74"),
            ("0.60", "1.68"), ("0.99", "0.07"),
        ],
    )
    def test_hundred_contract_fees(
        self, price: str, expected_dollars: str, schedule: FeeSchedule
    ) -> None:
        cents = taker_fee_cents(price, "100", schedule=schedule)
        ceiled = (cents / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
        assert ceiled == Decimal(expected_dollars)


class TestShape:
    def test_fee_is_symmetric_around_fifty_cents(self, schedule: FeeSchedule) -> None:
        for p in ("0.30", "0.10", "0.45"):
            mirror = str(1 - Decimal(p))
            assert taker_fee_cents(p, "100", schedule=schedule) == taker_fee_cents(
                mirror, "100", schedule=schedule
            )

    def test_fee_peaks_at_fifty_cents(self, schedule: FeeSchedule) -> None:
        peak = taker_fee_cents("0.50", "100", schedule=schedule)
        for p in ("0.10", "0.25", "0.75", "0.90"):
            assert taker_fee_cents(p, "100", schedule=schedule) < peak

    def test_matches_the_closed_form(self, schedule: FeeSchedule) -> None:
        p, c = Decimal("0.37"), Decimal("250")
        exact = Decimal("0.07") * c * p * (Decimal(1) - p) * Decimal(100)
        got = taker_fee_cents(p, c, schedule=schedule)
        assert got >= exact
        assert got - exact < Decimal("0.01")  # within one centicent


# ---------------------------------------------------------------------------
# Series lookup — replaces the old category model
# ---------------------------------------------------------------------------


class TestSeriesLookup:
    def test_unlisted_series_uses_the_default_taker_multiplier(
        self, schedule: FeeSchedule
    ) -> None:
        """Unlisted is not an error. The schedule lists only exceptions."""
        assert taker_fee_cents("0.50", "100", "KXNOTLISTED", schedule) == Decimal(175)

    def test_listed_series_at_multiplier_one_is_the_standard_rate(
        self, schedule: FeeSchedule
    ) -> None:
        assert taker_fee_cents("0.50", "100", "KXCPI", schedule) == Decimal(175)

    def test_fee_free_series_costs_nothing(self, schedule: FeeSchedule) -> None:
        """KXBTCY and KXETHY really are listed at 0/0 in the schedule."""
        assert taker_fee_cents("0.50", "100", "KXBTCY", schedule) == 0
        assert maker_fee_cents("0.50", "100", "KXBTCY", schedule) == 0

    def test_lookup_is_case_insensitive(self, schedule: FeeSchedule) -> None:
        for spelling in ("KXBTCY", "kxbtcy", "KxBtCy"):
            assert taker_fee_cents("0.50", "100", spelling, schedule) == 0

    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("KXFEDDECISION-26JUL-H25", "KXFEDDECISION"),
            ("KXBTCY-26DEC31-B100", "KXBTCY"),
            ("KXCPI", "KXCPI"),
            ("", None),
            (None, None),
        ],
    )
    def test_series_of_extracts_the_series(
        self, ticker: str | None, expected: str | None
    ) -> None:
        assert series_of(ticker) == expected


class TestMakerFees:
    def test_maker_defaults_to_zero(self, schedule: FeeSchedule) -> None:
        """The documented default maker multiplier is 0.

        Most markets charge no maker fee. Charging one by default — as this
        engine used to — overstates the cost of every resting order.
        """
        assert maker_fee_cents("0.50", "100", "KXNOTLISTED", schedule) == 0

    def test_listed_series_do_charge_maker_fees(self, schedule: FeeSchedule) -> None:
        assert maker_fee_cents("0.50", "100", "KXCPI", schedule) == Decimal("43.75")

    def test_maker_is_a_quarter_of_taker_where_charged(
        self, schedule: FeeSchedule
    ) -> None:
        taker = taker_fee_cents("0.50", "100", "KXCPI", schedule)
        maker = maker_fee_cents("0.50", "100", "KXCPI", schedule)
        assert maker == taker / 4


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_an_unverified_schedule_is_flagged(self, unverified: FeeSchedule) -> None:
        assert unverified.is_verified is False

    def test_a_verified_schedule_is_flagged(self, schedule: FeeSchedule) -> None:
        assert schedule.is_verified is True
        assert schedule.verified_on == "2026-07-27"

    def test_default_is_safe_while_nothing_exceeds_the_default(
        self, schedule: FeeSchedule
    ) -> None:
        assert schedule.default_is_safe is True

    def test_a_premium_multiplier_makes_the_default_unsafe(self) -> None:
        """If a series is ever listed above the default, an *unlisted* series
        might be premium too — so it can no longer be assumed standard.

        This is the residual fail-closed guard. Today every listed multiplier
        is 0 or 1, so defaulting can only overstate a fee, which is safe.
        """
        sched = FeeSchedule.from_dict(
            {
                "meta": {"verified_on": "2026-07-27"},
                "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
                "series": {"KXPREMIUM": {"maker": 0, "taker": 2}},
            }
        )
        assert sched.default_is_safe is False
        with pytest.raises(UnknownSeries):
            taker_fee_cents("0.50", "100", "KXNOTLISTED", sched)
        # The listed one still prices fine.
        assert taker_fee_cents("0.50", "100", "KXPREMIUM", sched) == Decimal(350)

    def test_unverified_schedule_error_names_the_fix(self) -> None:
        assert "refresh_fee_schedule" in str(UnverifiedFeeSchedule())

    def test_a_malformed_series_entry_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            FeeSchedule.from_dict({"series": {"KXBAD": 1.5}})


# ---------------------------------------------------------------------------
# Units at the boundary
# ---------------------------------------------------------------------------


class TestUnits:
    def test_accepts_wire_format_dollar_strings(self, schedule: FeeSchedule) -> None:
        assert taker_fee_cents("0.5600", "100", schedule=schedule) > 0

    def test_sub_cent_price_is_not_rounded_away(self, schedule: FeeSchedule) -> None:
        """Tick size varies per market, so sub-cent prices are real.

        Note the size: at 100 contracts the centicent rounding absorbs a
        0.001 price difference entirely (both come to 175c), which is correct
        and not a precision loss. The difference has to exceed a centicent of
        fee before it can show up at all.
        """
        a = taker_fee_cents("0.500000", "100000", schedule=schedule)
        b = taker_fee_cents("0.499000", "100000", schedule=schedule)
        assert a != b

    def test_fractional_contracts_are_supported(self, schedule: FeeSchedule) -> None:
        """2.5 contracts at 50c is $0.04375 — 437.5 centicents, which is half
        a centicent short of a whole one, so it rounds up to 4.38c."""
        assert taker_fee_cents("0.50", "2.50", schedule=schedule) == Decimal("4.38")

    def test_fee_is_monotonic_in_size(self, schedule: FeeSchedule) -> None:
        prev = Decimal(-1)
        for c in ("0.01", "0.5", "1", "10", "100"):
            fee = taker_fee_cents("0.50", c, schedule=schedule)
            assert fee >= prev
            prev = fee

    def test_decimal_and_string_inputs_agree(self, schedule: FeeSchedule) -> None:
        assert taker_fee_cents(Decimal("0.42"), Decimal(70), schedule=schedule) == (
            taker_fee_cents("0.42", "70", schedule=schedule)
        )

    @pytest.mark.parametrize("bad", ["56", "0", "1", "-0.5", "1.5"])
    def test_cents_style_price_is_rejected(
        self, bad: str, schedule: FeeSchedule
    ) -> None:
        """`56` must never be read as $56."""
        with pytest.raises(ValueError, match="between 0 and 1"):
            taker_fee_cents(bad, "100", schedule=schedule)

    def test_unparseable_price_raises(self, schedule: FeeSchedule) -> None:
        with pytest.raises(ValueError):
            taker_fee_cents("banana", "100", schedule=schedule)

    def test_negative_contracts_rejected(self, schedule: FeeSchedule) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            taker_fee_cents("0.50", "-1", schedule=schedule)

    def test_zero_contracts_is_free(self, schedule: FeeSchedule) -> None:
        assert taker_fee_cents("0.50", "0", schedule=schedule) == 0
        assert maker_fee_cents("0.50", "0", "KXCPI", schedule) == 0

    def test_fees_come_back_as_decimal_not_int(self, schedule: FeeSchedule) -> None:
        """An int return silently rounded every fee in the system."""
        assert isinstance(taker_fee_cents("0.50", "1", schedule=schedule), Decimal)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_charges_both_legs(self, schedule: FeeSchedule) -> None:
        both = round_trip_cost_cents(
            "0.40", "100", schedule=schedule, exit_price_dollars="0.60"
        )
        entry = taker_fee_cents("0.40", "100", schedule=schedule)
        assert both == entry + taker_fee_cents("0.60", "100", schedule=schedule)

    def test_holding_to_settlement_pays_no_exit_fee(
        self, schedule: FeeSchedule
    ) -> None:
        assert round_trip_cost_cents("0.40", "100", schedule=schedule) == (
            taker_fee_cents("0.40", "100", schedule=schedule)
        )

    def test_resting_entry_is_free_on_an_unlisted_series(
        self, schedule: FeeSchedule
    ) -> None:
        """Maker default is 0, so a resting entry costs nothing to open."""
        assert (
            round_trip_cost_cents(
                "0.40", "100", "KXNOTLISTED", entry_is_taker=False, schedule=schedule
            )
            == 0
        )


class TestNetEdge:
    def test_subtracts_fees_from_gross(self, schedule: FeeSchedule) -> None:
        # Gross 5c; fee at 0.50 over 100 contracts is 1.75c each.
        edge = net_edge_cents("0.55", "0.50", "100", schedule=schedule)
        assert edge == Decimal("3.25")

    def test_includes_slippage(self, schedule: FeeSchedule) -> None:
        edge = net_edge_cents(
            "0.55", "0.50", "100", slippage_cents="0.5", schedule=schedule
        )
        assert edge == Decimal("2.75")

    def test_a_thin_gross_edge_can_be_net_negative(
        self, schedule: FeeSchedule
    ) -> None:
        assert net_edge_cents("0.51", "0.50", "100", schedule=schedule) < 0

    def test_a_fee_free_series_keeps_the_whole_gross_edge(
        self, schedule: FeeSchedule
    ) -> None:
        assert net_edge_cents("0.55", "0.50", "100", "KXBTCY", schedule=schedule) == (
            Decimal("5.00")
        )

    def test_zero_size_has_no_edge(self, schedule: FeeSchedule) -> None:
        assert net_edge_cents("0.55", "0.50", "0", schedule=schedule) == 0
