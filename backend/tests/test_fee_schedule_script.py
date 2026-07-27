"""Tests for the fee-schedule verification guard.

Regression coverage for two guards that failed in different ways:

1. ``--mark-verified`` once used a string split on ``"categories:"`` to look
   for unresolved multipliers. That substring also appears inside
   ``maker_free_categories:``, so the check inspected the wrong block and
   stamped a schedule "verified" while a multiplier was still null.
2. The whole ``categories:`` model was wrong — the schedule is keyed by
   series ticker. The script's *guidance* pointed at a structure the PDF does
   not have, which is how ~50,000 markets ended up excluded from proposals
   over a multiplier that never existed.

A safety guard that inspects the wrong thing is worse than no guard, because
it looks like protection.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = next(
    p
    for p in (Path(__file__).resolve().parents[2], Path("/app"))
    if (p / "scripts" / "refresh_fee_schedule.py").is_file()
)
SCRIPT = REPO_ROOT / "scripts" / "refresh_fee_schedule.py"


def load_script(schedule_path: Path):
    """Import the script with its schedule path pointed at a fixture."""
    spec = importlib.util.spec_from_file_location("refresh_fee_schedule", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_fee_schedule"] = module
    spec.loader.exec_module(module)
    module.SCHEDULE_PATH = schedule_path
    return module


COMPLETE = """
meta:
  verified_on: null
  schedule_revision: null
formula:
  base_taker_rate: 0.07
  base_maker_rate: 0.0175
  rounding_increment_dollars: 0.0001
defaults:
  taker_multiplier: 1
  maker_multiplier: 0
series:
  KXCPI: {maker: 1, taker: 1}
  KXBTCY: {maker: 0, taker: 0}
"""


class TestProblemDetection:
    def test_a_complete_schedule_has_no_problems(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(COMPLETE)
        assert load_script(path).schedule_problems() == []

    def test_an_empty_series_table_is_a_problem(self, tmp_path: Path) -> None:
        """The table is the whole point; an empty one means nothing was pasted."""
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(
            COMPLETE.replace(
                "series:\n  KXCPI: {maker: 1, taker: 1}\n"
                "  KXBTCY: {maker: 0, taker: 0}\n",
                "series: {}\n",
            )
        )
        problems = load_script(path).schedule_problems()
        assert any("series table is empty" in p for p in problems)

    def test_a_missing_maker_multiplier_is_a_problem(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(
            COMPLETE.replace("KXCPI: {maker: 1, taker: 1}", "KXCPI: {taker: 1}")
        )
        problems = load_script(path).schedule_problems()
        assert any("KXCPI has no maker multiplier" in p for p in problems)

    def test_a_missing_taker_multiplier_is_a_problem(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(
            COMPLETE.replace("KXCPI: {maker: 1, taker: 1}", "KXCPI: {maker: 1}")
        )
        problems = load_script(path).schedule_problems()
        assert any("KXCPI has no taker multiplier" in p for p in problems)

    @pytest.mark.parametrize(
        "key", ["base_taker_rate", "base_maker_rate", "rounding_increment_dollars"]
    )
    def test_a_missing_formula_constant_is_a_problem(
        self, key: str, tmp_path: Path
    ) -> None:
        """The rounding increment especially: getting it wrong made every fee
        in the system ~14% high on small orders for three milestones."""
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(
            "\n".join(
                line
                for line in COMPLETE.splitlines()
                if not line.strip().startswith(key)
            )
        )
        problems = load_script(path).schedule_problems()
        assert any(key in p for p in problems)

    @pytest.mark.parametrize("key", ["taker_multiplier", "maker_multiplier"])
    def test_a_missing_default_is_a_problem(self, key: str, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(
            "\n".join(
                line
                for line in COMPLETE.splitlines()
                if not line.strip().startswith(key)
            )
        )
        problems = load_script(path).schedule_problems()
        assert any(key in p for p in problems)


class TestMarkVerifiedGuard:
    def test_refuses_while_the_table_is_incomplete(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(
            COMPLETE.replace("KXCPI: {maker: 1, taker: 1}", "KXCPI: {taker: 1}")
        )
        module = load_script(path)

        with pytest.raises(SystemExit) as exc:
            module.mark_verified(None)
        assert exc.value.code == 1

        # And critically: the file must be untouched.
        assert "verified_on: null" in path.read_text()

    def test_stamps_date_once_everything_is_resolved(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(COMPLETE)
        module = load_script(path)

        module.mark_verified(None)
        text = path.read_text()
        assert "verified_on: null" not in text
        assert "verified_on: 20" in text

    def test_records_revision_when_given(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(COMPLETE)
        module = load_script(path)

        module.mark_verified("July 7, 2026")
        assert "July 7, 2026" in path.read_text()
