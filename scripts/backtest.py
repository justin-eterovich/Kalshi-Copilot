#!/usr/bin/env python3
"""Replay a strategy over the book snapshots this deployment stored.

    docker compose run --rm --no-deps api python scripts/backtest.py --days 30
    docker compose run --rm --no-deps api python scripts/backtest.py \\
        --days 30 --max-price 10 --ignore-coverage

**It will usually refuse, and the refusal is the useful output.** There is no
historical orderbook endpoint at Kalshi, so the only book data that will ever
exist for a past moment is the snapshot ingest happened to take. Until this
system has been running long enough to have taken a lot of them, across a lot
of markets, and to have watched those markets resolve, there is nothing here
that can distinguish a strategy from luck. The coverage audit says so in
specifics — which markets, which span, which threshold — rather than printing
a Sharpe ratio nobody should believe.

``--ignore-coverage`` runs it anyway. That is a legitimate thing to want while
writing a strategy; it is not evidence, and the refusals stay on the report.

The shipped strategy is a **longshot test**: buy anything quoted at or below
``--max-price`` cents and hold it to settlement. It is here because it needs
nothing but a book and an outcome, and because it is the direct test of the
question `detectors/longshot_calibration.py` exists to ask — do 5c contracts
win 5% of the time? Net of fees, they have to win rather more than that.
"""

from __future__ import annotations

import argparse
import asyncio
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

from app.backtest import engine, replay  # noqa: E402
from app.config import get_config  # noqa: E402
from app.db.base import get_session_factory  # noqa: E402


def longshot_strategy(
    *, max_price: Decimal, contracts: Decimal
) -> replay.Strategy:
    """Buy YES at or below ``max_price``, once per market, and hold.

    Deliberately naive. It exists to exercise the harness end to end and to
    put a number on the longshot question, not to make money — and its one
    interesting property is that it needs no lookahead at all: the decision
    uses only the book in front of it.

    One position per market, **ever** — not "one while flat". Two reasons,
    and the second is the subtle one:

    - Without it, this would re-buy on every snapshot of the same market,
      turning one thesis into two dozen correlated positions and dividing the
      honest sample size by the same factor.
    - A flat check is not enough, because after settlement the position is
      gone from ``state.positions`` and a later snapshot of that market would
      buy it again — with the outcome already public. The replay raises
      ``LookAheadError`` rather than letting that become a number, which is
      the guard working; but a strategy should not be relying on it.
    """
    traded: set[str] = set()

    def strategy(state: replay.StrategyState) -> list[replay.Intent]:
        book = state.observation.book
        if not book or state.observation.ticker in traded:
            return []

        # The cheapest YES offer is the best NO bid complemented — the same
        # single book, read from the other side. `levels_for` in
        # app.trading.paper owns that mapping; here we only need to know
        # whether anything is offered at all, and at what price.
        offers = book.get("no") or []
        if not offers:
            return []
        best_yes_ask = Decimal(1) - max(Decimal(str(p)) for p, _ in offers)
        if not (Decimal(0) < best_yes_ask <= max_price):
            return []

        traded.add(state.observation.ticker)
        return [
            replay.Intent(
                ticker=state.observation.ticker,
                side=replay.Side.YES,
                action="buy",
                limit_price=max_price,
                contracts=contracts,
            )
        ]

    return strategy


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="window to replay")
    parser.add_argument(
        "--max-price",
        type=int,
        default=10,
        help="buy YES at or below this many cents",
    )
    parser.add_argument("--contracts", type=str, default="10")
    parser.add_argument(
        "--ignore-coverage",
        action="store_true",
        help="replay even when the coverage audit refuses (not evidence)",
    )
    args = parser.parse_args()

    config = get_config()
    now = datetime.now(UTC)
    window_start = now - timedelta(days=args.days)

    sessions = get_session_factory()
    async with sessions() as session:
        result = await engine.run_backtest(
            session,
            strategy=longshot_strategy(
                max_price=Decimal(args.max_price) / Decimal(100),
                contracts=Decimal(args.contracts),
            ),
            window_start=window_start,
            window_end=now,
            slippage_cents=Decimal(str(config.costs.slippage_buffer_cents)),
            min_trades=config.backtest.report_card_min_trades,
            ignore_coverage=args.ignore_coverage,
        )

    for line in result.summary_lines():
        print(line)

    # Non-zero when the data could not support a conclusion, so this is
    # usable from a script without parsing prose.
    return 0 if result.coverage.usable else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
