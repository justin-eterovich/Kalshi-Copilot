"""Tests for the signal duplicate guard.

A detector re-derives the same opportunity on every scan. The undervalued
screener wrote 180 near-identical rows in nine passes on a live run, and a
signals table nobody reads is the approval-fatigue failure one step earlier in
the pipeline.

The judgement being protected here is the threshold: fold a repeat, but never
fold away an edge that actually moved.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Config
from app.detectors.base import is_material_change


class TestIsMaterialChange:
    def test_a_first_sighting_is_always_material(self) -> None:
        assert is_material_change(
            None, Decimal("0"), threshold_cents=Decimal(1)
        )

    def test_an_identical_edge_is_not_material(self) -> None:
        assert not is_material_change(
            Decimal("2.5"), Decimal("2.5"), threshold_cents=Decimal(1)
        )

    def test_a_drift_below_the_threshold_is_not_material(self) -> None:
        """Books move constantly; a fraction of a cent is not news."""
        assert not is_material_change(
            Decimal("2.50"), Decimal("2.90"), threshold_cents=Decimal(1)
        )

    def test_a_real_move_is_material(self) -> None:
        """The case the guard must never swallow: 1c becoming 8c."""
        assert is_material_change(
            Decimal("1"), Decimal("8"), threshold_cents=Decimal(1)
        )

    def test_the_threshold_is_inclusive(self) -> None:
        assert is_material_change(
            Decimal("1"), Decimal("2"), threshold_cents=Decimal(1)
        )
        assert not is_material_change(
            Decimal("1"), Decimal("1.99"), threshold_cents=Decimal(1)
        )

    def test_a_shrinking_edge_is_material_too(self) -> None:
        """An edge that collapsed is as much news as one that grew — it means
        the opportunity is gone, which the operator needs to see."""
        assert is_material_change(
            Decimal("8"), Decimal("1"), threshold_cents=Decimal(1)
        )

    def test_a_research_detector_never_reports_a_change(self) -> None:
        """Research detectors always report zero edge, so every repeat folds
        and the screener cannot flood the table."""
        assert not is_material_change(
            Decimal(0), Decimal(0), threshold_cents=Decimal(1)
        )

    def test_a_zero_threshold_disables_folding_entirely(self) -> None:
        """The comparison is `>=`, so a threshold of zero is always satisfied
        and every sighting is treated as new — including an identical one.

        That is the second way to turn the guard off, alongside
        `dedupe_window_sec: 0`. Pinned by a test because it falls out of the
        inclusive boundary rather than being written deliberately, and someone
        reading only the config comment would expect zero to mean "fold exact
        repeats only".
        """
        assert is_material_change(
            Decimal("2.5"), Decimal("2.5"), threshold_cents=Decimal(0)
        )


class TestDedupeConfig:
    def test_the_defaults_fold_repeats(self) -> None:
        cfg = Config()
        assert cfg.detectors.dedupe_window_sec > 0
        assert cfg.detectors.dedupe_edge_change_cents > 0

    def test_folding_can_be_disabled(self) -> None:
        cfg = Config.model_validate({"detectors": {"dedupe_window_sec": 0}})
        assert cfg.detectors.dedupe_window_sec == 0

    @pytest.mark.parametrize("bad", [-1, -900])
    def test_a_negative_window_is_refused(self, bad: int) -> None:
        with pytest.raises(ValueError):
            Config.model_validate({"detectors": {"dedupe_window_sec": bad}})

    def test_a_negative_threshold_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Config.model_validate(
                {"detectors": {"dedupe_edge_change_cents": -1.0}}
            )

    def test_the_shipped_config_folds_repeats(self) -> None:
        """The screener is unusable without this; it ships on."""
        from app.config import load_config

        cfg = load_config("/app/config.yaml")
        assert cfg.detectors.dedupe_window_sec >= 60
