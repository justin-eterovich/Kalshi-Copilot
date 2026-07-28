"""Tests for the tuning dossier.

The dossier is what a human reads when the gate refuses and what the model
reads when it does not, so its failures are failures of *presentation* — and
the two that matter are both about hiding a distinction:

- averaging a resolved outcome together with a mark-to-market, producing a
  number belonging to no book that exists; and
- truncating pick rows silently, handing the reader a biased sample of the
  detector's own evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Config
from app.core.fees import FeeSchedule
from app.db.models import Side
from app.tuning import coverage, dossier
from app.tuning.mark import MarkBasis, Pick, Quote, Unmarkable, mark_pick
from app.tuning.window import window_for

VERIFIED = FeeSchedule.from_dict(
    {
        "meta": {"verified_on": "2026-07-27"},
        "formula": {"base_taker_rate": "0.07", "base_maker_rate": "0.0175"},
        "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
        "series": {},
    }
)

NOW = datetime(2026, 7, 28, 6, 0, tzinfo=UTC)
CREATED = NOW - timedelta(hours=36)


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {
            "costs": {"slippage_buffer_cents": 0.5, "assume_taker": True},
            "detectors": {"stale_quote": {"enabled": True, "min_net_edge_cents": 2.0}},
        }
    )


def marked(
    *,
    n: int,
    resolved: bool | None,
    claimed: str = "10",
    entry: str = "0.26",
    detector: str = "stale_quote",
    start_index: int = 0,
):
    """`n` marks on distinct tickers, all with the same shape."""
    out = []
    for i in range(n):
        idx = start_index + i
        pick = Pick(
            signal_id=idx,
            detector=detector,
            ticker=f"KXT-{idx}",
            side=Side.YES,
            action="buy",
            created_at=CREATED + timedelta(minutes=idx),
            claimed_edge_cents=Decimal(claimed),
            confidence=0.5,
            entry_price=Decimal(entry),
            contracts=Decimal("10"),
            event_ticker=f"KXE-{idx % 8}",
            became_proposal=idx % 5 == 0,
        )
        quote = Quote(
            ticker=pick.ticker,
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.44"),
            no_bid=Decimal("0.56"),
            no_ask=Decimal("0.60"),
            resolved_outcome=resolved,
        )
        out.append(mark_pick(pick, quote, schedule=VERIFIED))
    return out


class TestSummarise:
    def test_settled_and_open_are_reported_separately(self) -> None:
        marks = marked(n=5, resolved=True) + marked(
            n=5, resolved=None, start_index=100
        )
        agg = dossier.summarise("stale_quote", marks)

        assert agg.n_settled == 5
        assert agg.n_open == 5
        assert agg.realised_mean_settled != agg.realised_mean_open

    def test_the_mixed_figure_is_labelled_as_provisional(self) -> None:
        """It exists — it is the honest headline when little has settled — but
        nothing may mistake it for a realised number."""
        agg = dossier.summarise("stale_quote", marked(n=4, resolved=None))
        payload = agg.as_dict()

        assert payload["realised"]["mixed"]["n"] == 4
        assert "provisional" in payload["realised"]["mixed"]["basis"]
        assert "realised" in payload["realised"]["settled"]["basis"]

    def test_unmarkable_picks_do_not_enter_any_average(self) -> None:
        good = marked(n=3, resolved=None)
        bad = [
            mark_pick(
                Pick(
                    signal_id=999,
                    detector="stale_quote",
                    ticker="KXT-999",
                    side=Side.YES,
                    action="buy",
                    created_at=CREATED,
                    claimed_edge_cents=Decimal("500"),  # a wild claim
                    confidence=0.5,
                    entry_price=None,  # ...that cannot be scored
                    contracts=Decimal("10"),
                ),
                None,
                schedule=VERIFIED,
            )
        ]
        agg = dossier.summarise("stale_quote", good + bad)

        assert agg.n_mixed == 3
        # The 500c claim must not drag the claimed mean either: claims are
        # averaged over the same sample the realised figures are.
        assert agg.claimed_edge_mean == Decimal(10)

    def test_edge_error_is_claimed_minus_realised(self) -> None:
        agg = dossier.summarise("stale_quote", marked(n=4, resolved=None, claimed="20"))

        assert agg.claimed_edge_mean == Decimal(20)
        assert agg.realised_mean_mixed is not None
        assert agg.edge_error_mean == Decimal(20) - agg.realised_mean_mixed

    def test_fees_are_totalled_so_a_flat_strategy_shows_its_costs(self) -> None:
        agg = dossier.summarise("stale_quote", marked(n=4, resolved=None))

        assert agg.fees_paid_cents > 0


class TestSampleFor:
    def test_counts_reconcile(self) -> None:
        marks = marked(n=6, resolved=None)
        sample = dossier.sample_for("stale_quote", marks)

        assert sample.picks == 6
        assert sample.markable == 6
        assert sample.open_marks == 6
        assert sample.distinct_markets == 6

    def test_unmarkable_reasons_are_counted_not_dropped(self) -> None:
        pick = Pick(
            signal_id=1,
            detector="resolution_sniper",
            ticker="KXT-1",
            side=Side.NO,
            action="buy",
            created_at=CREATED,
            claimed_edge_cents=Decimal("0"),
            confidence=0.1,
            entry_price=None,
            contracts=None,
        )
        sample = dossier.sample_for(
            "resolution_sniper", [mark_pick(pick, None, schedule=VERIFIED)]
        )

        assert sample.markable == 0
        assert sample.unmarkable_by_reason == {Unmarkable.NO_ENTRY_PRICE.value: 1}

    def test_events_collapse_correlated_markets(self) -> None:
        """Twelve legs of one event are twelve markets and one outcome."""
        marks = marked(n=16, resolved=None)  # event = idx % 8
        sample = dossier.sample_for("stale_quote", marks)

        assert sample.distinct_markets == 16
        assert sample.distinct_events == 8


class TestBuildDossier:
    def _payload(self, config: Config, marks: list) -> dict:
        grouped = dossier.marks_by_detector(marks)
        win = window_for(NOW)
        report = coverage.audit(
            [dossier.sample_for(n, m) for n, m in grouped.items()],
            window_start=win.start,
            window_end=win.end,
            as_of=win.as_of,
        )
        return dossier.build_dossier(
            grouped, config=config, window=win, coverage=report
        )

    def test_money_is_serialised_as_strings(self, config: Config) -> None:
        """Parsing these into a JS number re-introduces the precision loss the
        whole backend exists to avoid."""
        payload = self._payload(config, marked(n=3, resolved=None))
        entry = payload["detectors"][0]
        row = entry["picks"][0]

        assert isinstance(row["entry_price"], str)
        assert isinstance(row["net_cents_per_contract"], str)
        assert isinstance(entry["performance"]["claimed_edge_cents_mean"], str)

    def test_the_refusals_travel_with_the_dossier(self, config: Config) -> None:
        payload = self._payload(config, marked(n=3, resolved=None))
        cov = payload["detectors"][0]["coverage"]

        assert cov["tunable"] is False
        assert any(r["code"] == "too_few_picks" for r in cov["refusals"])
        # Each refusal carries its measurement, not just a label.
        assert all(r["measured"] and r["threshold"] for r in cov["refusals"])

    def test_pick_rows_are_capped_towards_the_boldest_claims(
        self, config: Config
    ) -> None:
        """A cap needs an ordering, or it truncates in arrival order.

        The question a dossier answers is "which claims were wrong", so the
        boldest claims survive the cap.
        """
        quiet = marked(n=10, resolved=None, claimed="1")
        loud = marked(n=3, resolved=None, claimed="90", start_index=500)
        payload = self._payload(config, quiet + loud)

        entry = payload["detectors"][0]
        rows = entry["picks"]
        assert entry["picks_total"] == 13

        capped = dossier.build_dossier(
            dossier.marks_by_detector(quiet + loud),
            config=config,
            window=window_for(NOW),
            coverage=coverage.audit(
                [dossier.sample_for("stale_quote", quiet + loud)],
                window_start=window_for(NOW).start,
                window_end=window_for(NOW).end,
                as_of=NOW,
            ),
            max_pick_rows=3,
        )
        kept = capped["detectors"][0]

        assert kept["picks_truncated"] is True
        assert len(kept["picks"]) == 3
        assert {r["claimed_edge_cents"] for r in kept["picks"]} == {"90"}
        # The aggregates still describe every pick, not the surviving three.
        assert kept["counts"]["picks"] == 13
        assert rows  # the uncapped call kept them all

    def test_a_hypothetical_pick_says_so(self, config: Config) -> None:
        payload = self._payload(config, marked(n=3, resolved=None))
        rows = payload["detectors"][0]["picks"]

        assert any(r["hypothetical"] for r in rows)

    def test_the_current_config_is_included(self, config: Config) -> None:
        payload = self._payload(config, marked(n=3, resolved=None))
        entry = payload["detectors"][0]

        assert entry["config"]["min_net_edge_cents"] == 2.0
        assert entry["enabled"] is True

    def test_costs_are_stated_so_the_reader_knows_what_net_means(
        self, config: Config
    ) -> None:
        payload = self._payload(config, marked(n=3, resolved=None))

        assert payload["costs"]["slippage_buffer_cents"] == "0.5"
        assert "after fees" in payload["costs"]["note"]

    def test_unmarkable_rows_carry_their_reason_instead_of_a_pnl(
        self, config: Config
    ) -> None:
        pick = Pick(
            signal_id=7,
            detector="stale_quote",
            ticker="KXT-7",
            side=Side.YES,
            action="buy",
            created_at=CREATED,
            claimed_edge_cents=Decimal("5"),
            confidence=0.4,
            entry_price=Decimal("0.26"),
            contracts=Decimal("10"),
        )
        mark = mark_pick(pick, None, schedule=VERIFIED)
        payload = self._payload(config, [mark])
        row = payload["detectors"][0]["picks"][0]

        assert row["basis"] == MarkBasis.UNMARKABLE.value
        assert row["refusal"] == Unmarkable.MARKET_MISSING.value
        assert "net_cents_per_contract" not in row
