"""Tests for the stale-quote and resolution-sniper detectors.

Both of these are easy to build in a form that looks clever and loses money,
and the tests are mostly about the refusals rather than the signals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.detectors.resolution_sniper import assess
from app.detectors.stale_quote import (
    decisive_fair_price,
    parse_spot,
    reference_is_fresh,
    reference_symbol_for,
    resolve_strike,
)

# ---------------------------------------------------------------------------
# Strike resolution
# ---------------------------------------------------------------------------


class TestResolveStrike:
    def test_greater_holds_above_the_floor(self) -> None:
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(63000),
        )
        assert v is not None and v.yes is True
        # Margin is relative to spot: how far spot must fall to reach the
        # strike, which is the risk-relevant measure. 3000/63000 = 4.76%.
        assert v.margin_pct == pytest.approx(Decimal("4.7619"), abs=Decimal("0.001"))

    def test_greater_fails_below_the_floor(self) -> None:
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(57000),
        )
        assert v is not None and v.yes is False

    def test_greater_is_strict_at_the_boundary(self) -> None:
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(60000),
        )
        assert v is not None and v.yes is False
        assert v.margin_pct == 0

    def test_greater_or_equal_includes_the_boundary(self) -> None:
        v = resolve_strike(
            strike_type="greater_or_equal", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(60000),
        )
        assert v is not None and v.yes is True

    def test_less_uses_the_cap(self) -> None:
        v = resolve_strike(
            strike_type="less", floor_strike=None,
            cap_strike=Decimal(60000), spot=Decimal(57000),
        )
        assert v is not None and v.yes is True

    def test_between_holds_inside_the_band(self) -> None:
        v = resolve_strike(
            strike_type="between", floor_strike=Decimal(59000),
            cap_strike=Decimal(61000), spot=Decimal(60000),
        )
        assert v is not None and v.yes is True

    def test_between_uses_the_nearer_boundary_for_margin(self) -> None:
        """Inside a band the risk is whichever edge is closest."""
        v = resolve_strike(
            strike_type="between", floor_strike=Decimal(59000),
            cap_strike=Decimal(61000), spot=Decimal(60900),
        )
        assert v is not None
        assert v.margin_pct < Decimal(1)

    def test_between_fails_outside_the_band(self) -> None:
        v = resolve_strike(
            strike_type="between", floor_strike=Decimal(59000),
            cap_strike=Decimal(61000), spot=Decimal(62000),
        )
        assert v is not None and v.yes is False

    def test_custom_strikes_are_refused(self) -> None:
        """`custom` carries its own rules text. Guessing is not an option."""
        assert resolve_strike(
            strike_type="custom", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(63000),
        ) is None

    def test_an_unknown_strike_type_is_refused(self) -> None:
        """Not an invitation to assume `greater`."""
        assert resolve_strike(
            strike_type="somethingnew", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(63000),
        ) is None

    def test_a_missing_boundary_is_refused(self) -> None:
        assert resolve_strike(
            strike_type="greater", floor_strike=None,
            cap_strike=None, spot=Decimal(63000),
        ) is None

    def test_a_nonpositive_spot_is_refused(self) -> None:
        assert resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(0),
        ) is None


# ---------------------------------------------------------------------------
# Fair value
# ---------------------------------------------------------------------------


class TestDecisiveFairPrice:
    def test_a_thin_margin_is_not_decisive(self) -> None:
        """BTC moves percent in minutes. 0.2% past a strike decides nothing."""
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal("60120"),
        )
        assert v is not None
        assert decisive_fair_price(v, min_margin_pct=Decimal(1)) is None

    def test_a_wide_margin_prices_near_certainty(self) -> None:
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(66000),
        )
        assert v is not None
        assert decisive_fair_price(v, min_margin_pct=Decimal(1)) == Decimal("0.98")

    def test_a_breached_strike_prices_near_zero(self) -> None:
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(54000),
        )
        assert v is not None
        assert decisive_fair_price(v, min_margin_pct=Decimal(1)) == Decimal("0.02")

    def test_fair_value_is_never_certainty(self) -> None:
        """Pricing at 1.00 would manufacture the last cents of edge from
        nothing. The outcome is very likely, not settled."""
        v = resolve_strike(
            strike_type="greater", floor_strike=Decimal(60000),
            cap_strike=None, spot=Decimal(120000),
        )
        assert v is not None
        fair = decisive_fair_price(v, min_margin_pct=Decimal(1))
        assert fair is not None and fair < Decimal(1)


class TestReferenceFreshness:
    def test_a_recent_observation_is_fresh(self) -> None:
        assert reference_is_fresh(
            datetime.now(UTC) - timedelta(seconds=2), max_age_sec=5
        )

    def test_a_stale_observation_is_not(self) -> None:
        """A stale reference against a live market invents an edge in
        whichever direction the market already moved."""
        assert not reference_is_fresh(
            datetime.now(UTC) - timedelta(seconds=60), max_age_sec=5
        )

    def test_a_missing_observation_is_not_fresh(self) -> None:
        assert not reference_is_fresh(None, max_age_sec=5)

    def test_a_naive_timestamp_is_treated_as_utc(self) -> None:
        naive = datetime.now(UTC).replace(tzinfo=None)
        assert reference_is_fresh(naive, max_age_sec=30)


class TestParseSpot:
    def test_accepts_a_decimal_string(self) -> None:
        assert parse_spot("63000.12") == Decimal("63000.12")

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_refuses_a_nonpositive_price(self, bad: str) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse_spot(bad)

    def test_refuses_nonsense(self) -> None:
        with pytest.raises(ValueError):
            parse_spot("not-a-price")


# ---------------------------------------------------------------------------
# Resolution sniper
# ---------------------------------------------------------------------------


def past(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def future(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def sniff(**overrides: object):
    kwargs: dict = {
        "ticker": "KX-A",
        "yes_bid": Decimal("0.96"),
        "yes_ask": Decimal("0.98"),
        "close_time": past(10),
        "yes_threshold_cents": Decimal(97),
        "no_threshold_cents": Decimal(3),
    }
    kwargs.update(overrides)
    return assess(**kwargs)  # type: ignore[arg-type]


class TestResolutionSniper:
    def test_a_market_still_open_is_not_a_candidate(self) -> None:
        """Settlement lag needs the close to have happened."""
        assert sniff(close_time=future(30)) is None

    def test_a_market_with_no_close_time_is_not_a_candidate(self) -> None:
        assert sniff(close_time=None) is None

    def test_past_close_and_pinned_high_is_a_candidate(self) -> None:
        c = sniff()
        assert c is not None and c.side == "yes"
        assert c.price == Decimal("0.98")

    def test_the_threshold_is_on_the_executable_price(self) -> None:
        """A market quoted 96/99 is not a 97c opportunity — you pay 99."""
        assert sniff(yes_bid=Decimal("0.90"), yes_ask=Decimal("0.96")) is None

    def test_past_close_and_pinned_low_is_a_no_candidate(self) -> None:
        c = sniff(yes_bid=Decimal("0.02"), yes_ask=Decimal("0.04"))
        assert c is not None and c.side == "no"
        assert c.price == Decimal("0.98")

    def test_a_mid_priced_market_is_not_a_candidate(self) -> None:
        assert sniff(yes_bid=Decimal("0.48"), yes_ask=Decimal("0.52")) is None

    def test_confidence_is_capped_low(self) -> None:
        """No amount of waiting turns a price into a settlement source."""
        for minutes in (1, 60, 600, 6000):
            c = sniff(close_time=past(minutes))
            assert c is not None
            assert c.confidence <= 0.35

    def test_confidence_grows_with_time_past_close(self) -> None:
        early = sniff(close_time=past(1))
        late = sniff(close_time=past(120))
        assert early is not None and late is not None
        assert late.confidence > early.confidence

    def test_a_candidate_is_not_actionable_without_a_source(self) -> None:
        """The central refusal.

        A price of 98c is the crowd's opinion. Buying it because it is high
        is a 49:1 bet, not an arbitrage, and being wrong 3% of the time loses
        money steadily after fees.
        """
        c = sniff()
        assert c is not None
        assert c.source_confirmed is False
        assert c.actionable is False

    def test_only_a_confirmed_source_makes_it_actionable(self) -> None:
        c = sniff()
        assert c is not None
        confirmed = type(c)(
            ticker=c.ticker, side=c.side, price=c.price,
            minutes_past_close=c.minutes_past_close, confidence=c.confidence,
            source_confirmed=True,
        )
        assert confirmed.actionable is True


# ---------------------------------------------------------------------------
# Reference symbol resolution
#
# These exist because of a bug that reached a live run: the detector selected
# markets by `category == "Crypto"` and priced every one of them against
# BTC-USD. An ETH contract with a $1,969 strike, compared to Bitcoin at
# $65,154, resolved "decisively YES" and reported a 71-cent edge on a market
# genuinely quoted at 26c. Nothing downstream could have caught it: every
# number involved was valid, they just described different assets.
# ---------------------------------------------------------------------------


class TestReferenceSymbol:
    def test_bitcoin_series_map_to_btc(self) -> None:
        for ticker in (
            "KXBTC-26JUL2706-B68650",
            "KXBTCD-26JUL2706-T59699.99",
            "KXBTC15M-26JUL270530-30",
            "KXBTCY-26-T100000",
        ):
            assert reference_symbol_for(ticker) == "BTC-USD"

    def test_ether_series_do_not_map_to_bitcoin(self) -> None:
        """The exact case that produced the phantom 71-cent edge."""
        assert reference_symbol_for("KXETHD-26JUL2706-T1969.99") == "ETH-USD"

    def test_solana_and_xrp_are_their_own_underlyings(self) -> None:
        assert reference_symbol_for("KXSOLE-26JUL2706-B76.375") == "SOL-USD"
        assert reference_symbol_for("KXXRP-26JUL2706-T3.20") == "XRP-USD"

    def test_an_unknown_series_is_refused(self) -> None:
        """Not an invitation to fall back to BTC.

        A market priced against the wrong asset does not look broken, it
        looks like an enormous edge.
        """
        assert reference_symbol_for("KXNFLGAME-26SEP13CLEJAC-CLE") is None
        assert reference_symbol_for("KXCPI-26JUL-T3.5") is None

    def test_a_missing_ticker_is_refused(self) -> None:
        assert reference_symbol_for(None) is None
        assert reference_symbol_for("") is None

    def test_matching_is_on_the_series_not_a_substring(self) -> None:
        """A strike or date containing 'BTC' must not pull a market in."""
        assert reference_symbol_for("KXNFLGAME-26BTC13-CLE") is None

    def test_the_series_is_the_segment_before_the_first_hyphen(self) -> None:
        assert reference_symbol_for("kxethd-26jul2706-t1969.99") == "ETH-USD"
