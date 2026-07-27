"""Tests for the longshot-bias calibration screen.

The screen's failure mode is not a wrong number, it is a confident one: a
tiny bucket, a Wald interval that leaves [0, 1], and a rate that reads like a
strategy. So most of what follows is about what the module refuses to say.
"""

from __future__ import annotations

import pytest

from app.detectors.longshot_calibration import (
    BucketStat,
    Observation,
    bucket_for,
    calibrate,
    significant,
    wilson_interval,
)

LOW = (1, 10)
HIGH = (90, 99)


def obs(bucket: int, *, yes: int, no: int) -> list[Observation]:
    """`yes` winners and `no` losers, all in one bucket."""
    return [Observation(price_bucket_cents=bucket, settled_yes=True)] * yes + [
        Observation(price_bucket_cents=bucket, settled_yes=False)
    ] * no


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


class TestBucketFor:
    def test_a_price_in_the_low_band_buckets_to_its_cent_value(self) -> None:
        assert bucket_for(5, low_band=LOW, high_band=HIGH) == 5

    def test_a_price_in_the_high_band_buckets_to_its_cent_value(self) -> None:
        assert bucket_for(97, low_band=LOW, high_band=HIGH) == 97

    @pytest.mark.parametrize("edge", [1, 10, 90, 99])
    def test_band_edges_are_inclusive(self, edge: int) -> None:
        assert bucket_for(edge, low_band=LOW, high_band=HIGH) == edge

    def test_the_middle_of_the_book_is_not_bucketed(self) -> None:
        """Longshot bias is a claim about the tails; the middle is not sampled."""
        assert bucket_for(50, low_band=LOW, high_band=HIGH) is None

    def test_an_inverted_band_is_refused(self) -> None:
        """A transposed config must not read as 'no bias found'."""
        assert bucket_for(5, low_band=(10, 1), high_band=HIGH) is None

    def test_an_inverted_high_band_is_refused_even_on_a_low_match(self) -> None:
        """The refusal is about the config, not about this particular price."""
        assert bucket_for(5, low_band=LOW, high_band=(99, 90)) is None

    @pytest.mark.parametrize("bad", [0, 100, -5, 101])
    def test_a_price_outside_the_open_interval_is_refused(self, bad: int) -> None:
        """0c and 100c are settled markets, not probabilities."""
        assert bucket_for(bad, low_band=(0, 100), high_band=(0, 100)) is None


# ---------------------------------------------------------------------------
# Wilson interval
# ---------------------------------------------------------------------------


class TestWilsonInterval:
    def test_an_empty_sample_is_refused(self) -> None:
        assert wilson_interval(0, 0) is None

    def test_more_successes_than_trials_is_refused(self) -> None:
        """An upstream bug. Clamping it would launder the bug into a number."""
        assert wilson_interval(11, 10) is None

    def test_a_negative_success_count_is_refused(self) -> None:
        assert wilson_interval(-1, 10) is None

    def test_a_negative_sample_size_is_refused(self) -> None:
        assert wilson_interval(0, -10) is None

    def test_zero_successes_stays_inside_the_unit_interval(self) -> None:
        """The case Wald gets visibly wrong: it would go negative here."""
        interval = wilson_interval(0, 40)
        assert interval is not None
        low, high = interval
        assert low == 0.0
        assert 0.0 < high < 1.0

    def test_all_successes_stays_inside_the_unit_interval(self) -> None:
        interval = wilson_interval(40, 40)
        assert interval is not None
        low, high = interval
        assert high == 1.0
        assert 0.0 < low < 1.0

    @pytest.mark.parametrize("successes", [0, 1, 5, 50, 99, 100])
    def test_bounds_never_leave_the_unit_interval(self, successes: int) -> None:
        interval = wilson_interval(successes, 100)
        assert interval is not None
        low, high = interval
        assert 0.0 <= low <= high <= 1.0

    def test_it_matches_the_published_value(self) -> None:
        """Wilson at 6/20, z=1.96 — the textbook worked example."""
        interval = wilson_interval(6, 20)
        assert interval is not None
        low, high = interval
        assert low == pytest.approx(0.1455, abs=0.001)
        assert high == pytest.approx(0.5190, abs=0.001)

    def test_the_interval_narrows_as_the_sample_grows(self) -> None:
        small = wilson_interval(5, 50)
        large = wilson_interval(50, 500)
        assert small is not None and large is not None
        assert (large[1] - large[0]) < (small[1] - small[0])

    def test_it_is_not_the_normal_approximation(self) -> None:
        """At 1/50 Wald gives roughly (-0.019, 0.059). Wilson must not.

        This is the whole reason the module uses Wilson: a lower bound below
        zero in a 2c bucket is exactly where the strategy wants to act.
        """
        interval = wilson_interval(1, 50)
        assert interval is not None
        low, high = interval
        assert low > 0.0
        # Wilson is shifted towards 1/2, so the upper bound sits well above
        # Wald's.
        assert high > 0.06

    def test_a_wider_z_gives_a_wider_interval(self) -> None:
        narrow = wilson_interval(20, 100, z=1.96)
        wide = wilson_interval(20, 100, z=2.58)
        assert narrow is not None and wide is not None
        assert wide[0] < narrow[0] and wide[1] > narrow[1]


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class TestCalibrate:
    def test_no_observations_produce_no_buckets(self) -> None:
        assert calibrate([]) == []

    def test_the_implied_rate_of_a_bucket_is_its_price(self) -> None:
        stats = calibrate(obs(5, yes=10, no=90))
        assert len(stats) == 1
        assert stats[0].implied_rate == pytest.approx(0.05)

    def test_it_reports_the_observed_rate_and_counts(self) -> None:
        stats = calibrate(obs(5, yes=10, no=90))
        s = stats[0]
        assert s.samples == 100
        assert s.yes_count == 10
        assert s.observed_rate == pytest.approx(0.10)

    def test_buckets_come_back_sorted_by_price(self) -> None:
        observations = obs(97, yes=5, no=1) + obs(2, yes=1, no=9) + obs(90, yes=8, no=2)
        assert [s.bucket_cents for s in calibrate(observations)] == [2, 90, 97]

    def test_a_bucket_outside_the_open_interval_is_dropped(self) -> None:
        """A 0c or 100c row is a settled price that leaked in. Drop it."""
        observations = obs(0, yes=5, no=5) + obs(100, yes=5, no=5) + obs(5, yes=1, no=9)
        assert [s.bucket_cents for s in calibrate(observations)] == [5]

    def test_a_bucket_with_no_winners_is_still_reported(self) -> None:
        """0-for-n is real data, and Wilson handles it without leaving [0, 1]."""
        stats = calibrate(obs(3, yes=0, no=200))
        s = stats[0]
        assert s.observed_rate == 0.0
        assert s.ci_low == 0.0
        assert 0.0 < s.ci_high < 1.0

    def test_a_bucket_that_always_won_is_still_reported(self) -> None:
        stats = calibrate(obs(97, yes=200, no=0))
        s = stats[0]
        assert s.observed_rate == 1.0
        assert s.ci_high == 1.0
        assert 0.0 < s.ci_low < 1.0

    def test_thin_buckets_are_reported_not_hidden(self) -> None:
        """`calibrate` shows coverage. `significant` is what decides."""
        stats = calibrate(obs(5, yes=2, no=0))
        assert len(stats) == 1 and stats[0].samples == 2


class TestMispriced:
    def test_a_price_inside_the_interval_is_not_mispriced(self) -> None:
        """A 5c bucket settling YES 5.2% of the time over 1000 markets is a
        market doing its job, not an edge."""
        stats = calibrate(obs(5, yes=52, no=948))
        assert stats[0].mispriced is False

    def test_a_price_outside_the_interval_is_mispriced(self) -> None:
        """5c contracts winning 1% of 2000 is longshot bias, if the sample is
        honest."""
        stats = calibrate(obs(5, yes=20, no=1980))
        s = stats[0]
        assert s.ci_high < s.implied_rate
        assert s.mispriced is True

    def test_the_underpriced_favourite_side_is_symmetric(self) -> None:
        """The other half of the claim: 95c contracts winning 99% of the time."""
        stats = calibrate(obs(95, yes=1980, no=20))
        s = stats[0]
        assert s.ci_low > s.implied_rate
        assert s.mispriced is True

    def test_the_interval_endpoints_count_as_supported(self) -> None:
        """`mispriced` is 'outside', not 'not equal to'. A price sitting on the
        boundary is still a value the data supports."""
        s = BucketStat(
            bucket_cents=5, samples=1000, yes_count=30, observed_rate=0.03,
            implied_rate=0.05, ci_low=0.05, ci_high=0.09,
        )
        assert s.mispriced is False

    def test_a_differing_point_estimate_alone_is_not_mispricing(self) -> None:
        """Point estimates always differ. One winner in twelve is 8.3% against
        a 5c price, and the interval is wide enough to say that is noise."""
        stats = calibrate(obs(5, yes=1, no=11))
        assert stats[0].observed_rate == pytest.approx(0.0833, abs=0.001)
        assert stats[0].mispriced is False


# ---------------------------------------------------------------------------
# The sample floor — the only thing between this module and a bad trade
# ---------------------------------------------------------------------------


class TestSignificant:
    def test_a_bucket_below_the_sample_floor_never_signals(self) -> None:
        """Two-for-two at 5c looks like a 100% hit rate and means nothing."""
        stats = calibrate(obs(5, yes=2, no=0))
        assert stats[0].mispriced is True
        assert significant(stats, min_samples=500) == []

    def test_a_bucket_at_the_floor_qualifies(self) -> None:
        stats = calibrate(obs(5, yes=5, no=495))
        assert stats[0].samples == 500
        assert [s.bucket_cents for s in significant(stats, min_samples=500)] == [5]

    def test_a_large_but_well_calibrated_bucket_does_not_signal(self) -> None:
        stats = calibrate(obs(5, yes=100, no=1900))
        assert stats[0].samples >= 500
        assert significant(stats, min_samples=500) == []

    def test_a_large_and_mispriced_bucket_signals(self) -> None:
        stats = calibrate(obs(5, yes=20, no=1980))
        assert [s.bucket_cents for s in significant(stats, min_samples=500)] == [5]

    def test_thin_buckets_are_filtered_out_of_a_mixed_set(self) -> None:
        """The 2c bucket is 'mispriced' on three markets. The floor is what
        stops that reaching a caller."""
        observations = obs(2, yes=3, no=0) + obs(95, yes=1980, no=20)
        stats = calibrate(observations)
        assert [s.bucket_cents for s in stats] == [2, 95]
        assert [s.bucket_cents for s in significant(stats, min_samples=500)] == [95]

    def test_no_stats_produce_no_signals(self) -> None:
        assert significant([], min_samples=500) == []

    def test_a_zero_floor_disables_the_only_safety_mechanism(self) -> None:
        """Documenting the footgun rather than hiding it: with no floor, three
        markets at 2c that all happened to settle YES come back as a finding.
        The config default is 500 for this reason."""
        stats = calibrate(obs(2, yes=3, no=0))
        assert significant(stats, min_samples=0) != []
