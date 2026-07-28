#!/usr/bin/env python3
"""Score yesterday's detector picks and build the tuning dossier.

    docker compose run --rm --no-deps api python scripts/tune_detectors.py
    docker compose run --rm --no-deps api python scripts/tune_detectors.py \\
        --span-hours 168 --seasoning-hours 24 --json /tmp/dossier.json

**It will refuse on this deployment, and the refusal is the useful output.**
The signals table is younger than the seasoning lag, so the default window
``[T-48h, T-24h)`` is empty; and even once it fills, a detector needs 30 picks
across 10 markets and 5 events before a threshold suggestion drawn from it is
anything but noise. ``app/tuning/coverage.py`` names every measured number
against the floor it missed, because "insufficient data" does not tell an
operator whether to wait a week or change the window.

This is M10a: it harvests, scores, gates and reports. **No model is called and
no config is changed** — that is M10b and M10c, and neither should be built
until the numbers this prints have been read by a human and believed.

``--ignore-coverage`` builds the dossier anyway. Legitimate while developing;
it is not evidence, and the refusals stay attached to the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Two layouts: on the host the package is at `backend/app`; inside the image
# the working directory *is* the package root and `scripts/` sits beside it.
for candidate in (REPO_ROOT / "backend", REPO_ROOT):
    if (candidate / "app" / "__init__.py").is_file():
        sys.path.insert(0, str(candidate))
        break

from app.config import get_config  # noqa: E402
from app.db.base import get_session_factory  # noqa: E402
from app.detectors.base import enabled_detector_names  # noqa: E402
from app.tuning import coverage, dossier, harvest, window  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--span-hours",
        type=float,
        default=24.0,
        help="how much of the timeline to harvest (default 24)",
    )
    parser.add_argument(
        "--seasoning-hours",
        type=float,
        default=24.0,
        help=(
            "how long picks are left before marking (default 24). Longer means "
            "more settled outcomes and slower feedback."
        ),
    )
    parser.add_argument(
        "--detector",
        action="append",
        default=None,
        help="restrict to this detector; repeatable",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="write the full dossier here (this is the model's input in M10b)",
    )
    parser.add_argument(
        "--ignore-coverage",
        action="store_true",
        help="build the dossier even when every detector is refused",
    )
    args = parser.parse_args()

    config = get_config()
    win = window.window_for(
        datetime.now(UTC),
        seasoning=timedelta(hours=args.seasoning_hours),
        span=timedelta(hours=args.span_hours),
    )

    sessions = get_session_factory()
    async with sessions() as session:
        marks = await harvest.harvest(
            session,
            win,
            slippage_cents=Decimal(str(config.costs.slippage_buffer_cents)),
            detectors=args.detector,
            is_taker=config.costs.assume_taker,
        )

    grouped = dossier.marks_by_detector(marks)

    # An enabled detector that produced nothing must still appear, refused with
    # NO_PICKS. A detector missing from the report looks exactly like one that
    # ran and found nothing — the same failure the leaderboard_watcher stub
    # exists to prevent, and the reason the weather engine went unreported for
    # a milestone while it scanned.
    audited = set(grouped) | set(enabled_detector_names(config))
    if args.detector:
        audited &= set(args.detector)

    samples = [
        dossier.sample_for(name, grouped.get(name, [])) for name in sorted(audited)
    ]
    report = coverage.audit(
        samples,
        window_start=win.start,
        window_end=win.end,
        as_of=win.as_of,
    )

    for line in report.summary_lines():
        print(line)

    if not report.usable and not args.ignore_coverage:
        print()
        print(
            "No detector cleared its floors, so no dossier was written. This "
            "is the expected answer on this deployment today — the signals "
            "table is younger than the seasoning lag. Re-run with "
            "--ignore-coverage to write the dossier anyway (not evidence)."
        )
        return 2

    payload = dossier.build_dossier(
        grouped, config=config, window=win, coverage=report
    )

    if args.json:
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print()
        print(f"dossier written to {args.json}")
    else:
        print()
        for entry in payload["detectors"]:
            perf = entry["performance"]
            print(
                f"  {entry['detector']:<24} "
                f"claimed={perf['claimed_edge_cents_mean']} "
                f"realised(mixed)={perf['realised']['mixed']['mean_cents_per_contract']} "
                f"error={perf['edge_error_cents_mean']}"
            )
        print()
        print("Pass --json PATH to write the full dossier.")

    # Non-zero when the data could not support a conclusion, so this is usable
    # from a script without parsing prose.
    return 0 if report.usable else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
