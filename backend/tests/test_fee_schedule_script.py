"""Tests for the fee-schedule verification guard.

Regression coverage for a genuinely dangerous bug: ``--mark-verified`` used a
string split on ``"categories:"`` to look for unresolved multipliers. That
substring also appears inside ``maker_free_categories:``, so the check
inspected the wrong block and stamped a schedule "verified" while ``crypto``
was still null — silently clearing the UI warning that those markets cannot
be priced.

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


UNRESOLVED = """
meta:
  verified_on: null
  schedule_revision: null
formula:
  base_taker_rate: 0.07
  maker_rate_fraction: 0.25
categories:
  default: 1.0
  crypto: null
maker_free_categories: []
"""

RESOLVED = """
meta:
  verified_on: null
  schedule_revision: null
formula:
  base_taker_rate: 0.07
  maker_rate_fraction: 0.25
categories:
  default: 1.0
  crypto: 2.0
maker_free_categories: []
"""


class TestUnresolvedDetection:
    def test_finds_null_multiplier(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(UNRESOLVED)
        module = load_script(path)
        assert module.unresolved_categories() == ["crypto"]

    def test_reports_nothing_when_all_resolved(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(RESOLVED)
        module = load_script(path)
        assert module.unresolved_categories() == []

    def test_maker_free_categories_does_not_mask_the_real_block(
        self, tmp_path: Path
    ) -> None:
        """The exact shape that defeated the old string-splitting check.

        `maker_free_categories:` contains the substring `categories:`, so
        splitting on it landed past the real block and saw no nulls.
        """
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(UNRESOLVED.replace("maker_free_categories: []", "maker_free_categories:\n  - sports"))
        module = load_script(path)
        assert module.unresolved_categories() == ["crypto"]

    def test_multiple_unresolved_all_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(UNRESOLVED.replace("  crypto: null", "  crypto: null\n  sp500: null"))
        module = load_script(path)
        assert module.unresolved_categories() == ["crypto", "sp500"]


class TestMarkVerifiedGuard:
    def test_refuses_while_a_multiplier_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(UNRESOLVED)
        module = load_script(path)

        with pytest.raises(SystemExit) as exc:
            module.mark_verified(None)
        assert exc.value.code == 1

        # And critically: the file must be untouched.
        assert "verified_on: null" in path.read_text()

    def test_stamps_date_once_everything_is_resolved(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(RESOLVED)
        module = load_script(path)

        module.mark_verified(None)
        text = path.read_text()
        assert "verified_on: null" not in text
        assert "verified_on: 20" in text

    def test_records_revision_when_given(self, tmp_path: Path) -> None:
        path = tmp_path / "fee_schedule.yaml"
        path.write_text(RESOLVED)
        module = load_script(path)

        module.mark_verified("7.7.26")
        assert '"7.7.26"' in path.read_text()
