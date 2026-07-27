#!/usr/bin/env python3
"""Verify data/fee_schedule.yaml against Kalshi's official fee schedule PDF.

Run this from your homelab, not from a datacenter IP — kalshi.com sits behind
bot protection that returns a challenge page to cloud hosts.

    python scripts/refresh_fee_schedule.py            # fetch + report
    python scripts/refresh_fee_schedule.py --file X   # use a PDF you saved
    python scripts/refresh_fee_schedule.py --mark-verified

The script deliberately does NOT auto-write multipliers it is not sure about.
An invented multiplier silently inflates every edge number in the system, so
anything ambiguous is reported for you to enter by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = REPO_ROOT / "data" / "fee_schedule.yaml"
PDF_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def fetch_pdf(dest: Path) -> bool:
    """Download the schedule PDF. Returns False if we got a challenge page."""
    try:
        import httpx
    except ImportError:
        print("! httpx not installed. pip install httpx", file=sys.stderr)
        return False

    print(f"-> fetching {PDF_URL}")
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"},
        ) as client:
            resp = client.get(PDF_URL)
    except Exception as exc:
        print(f"! request failed: {exc}", file=sys.stderr)
        return False

    if resp.status_code != 200:
        print(f"! HTTP {resp.status_code}", file=sys.stderr)
        return False

    body = resp.content
    if not body.startswith(b"%PDF"):
        print(
            "! got HTML, not a PDF — almost certainly a bot-protection "
            "challenge.\n"
            "  Open the URL in a browser, save the PDF, then re-run with:\n"
            f"    python {Path(__file__).name} --file /path/to/kalshi-fee-schedule.pdf",
            file=sys.stderr,
        )
        return False

    dest.write_bytes(body)
    print(f"-> saved {len(body):,} bytes")
    return True


def pdf_to_text(pdf: Path) -> str | None:
    """Extract text using pdftotext, then pypdf, whichever is available."""
    if shutil.which("pdftotext"):
        out = pdf.with_suffix(".txt")
        try:
            subprocess.run(
                ["pdftotext", "-layout", str(pdf), str(out)],
                check=True,
                capture_output=True,
            )
            return out.read_text(encoding="utf-8", errors="replace")
        except subprocess.CalledProcessError:
            pass

    try:
        from pypdf import PdfReader
    except ImportError:
        print(
            "! no PDF text extractor available.\n"
            "  Install one:  pip install pypdf   (or: apt install poppler-utils)",
            file=sys.stderr,
        )
        return None

    reader = PdfReader(str(pdf))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def analyse(text: str) -> None:
    """Extract the formula constants and the non-standard series table.

    The schedule keys multipliers by **series ticker**, not by category. An
    earlier version of this script hunted for category names and told the
    operator to fill in a `categories:` block — a structure the PDF does not
    have. That guidance is how ~50,000 Crypto markets ended up excluded from
    proposals over a multiplier that never existed.
    """
    print("\n" + "=" * 70)
    print("FEE SCHEDULE FINDINGS")
    print("=" * 70)

    revision = re.search(
        r"Last\s+updated\s+and\s+effective:\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
        " ".join(text.split()),
        re.IGNORECASE,
    )
    print(f"\nschedule revision: {revision.group(1) if revision else 'not found'}")

    flat = " ".join(text.split())

    taker = re.search(r"fees\s*=\s*round\s*up\(\s*M\s*x\s*(0\.\d+)", flat, re.IGNORECASE)
    makers = re.findall(r"round\s*up\(\s*M\s*x\s*(0\.\d+)", flat, re.IGNORECASE)
    print(f"taker base rate : {taker.group(1) if taker else '?'}")
    print(f"maker base rate : {makers[1] if len(makers) > 1 else '?'}")

    rounding = re.search(r"round\s*up\s*=\s*rounds?\s*up[^.]{0,120}", flat, re.IGNORECASE)
    if rounding:
        print(f"rounding rule   : {rounding.group(0).strip()}")
        if "centicent" in rounding.group(0).lower():
            print("  ^^ NOTE: a CENTICENT is $0.0001. Fees are NOT whole cents.")

    for label, pattern in (
        ("taker M default", r"M\s*=\s*the\s*multiplier[^.]*?default\s*is\s*(\d)"),
    ):
        for m in re.finditer(pattern, flat, re.IGNORECASE):
            print(f"{label:16}: {m.group(1)}")

    # Strip every repeated page header and footer before matching rows. The
    # table spans pages, and leaving them in breaks the lookahead at each page
    # boundary — which silently drops a handful of series rather than failing.
    table = flat
    for junk in (
        "Non-Standard Fees Series Maker Multipler Taker Multiplier",
        "Series Maker Multipler Taker Multiplier",
        "Non-Standard Fees",
    ):
        table = table.replace(junk, " ")
    table = re.sub(
        r"Last\s+updated\s+and\s+effective:\s*[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}",
        " ",
        table,
    )
    rows = re.findall(
        r"(KX[A-Z0-9]+)\s+(.*?)\s+(\d+)\s+(\d+)(?=\s+KX|\s*$)", table
    )
    print(f"\nnon-standard series listed: {len(rows)}")
    if rows:
        combos: dict[tuple[str, str], int] = {}
        for _, _, mk, tk in rows:
            combos[(mk, tk)] = combos.get((mk, tk), 0) + 1
        for (mk, tk), n in sorted(combos.items()):
            note = (
                "no trading fees at all" if (mk, tk) == ("0", "0")
                else "standard taker, maker fees DO apply" if (mk, tk) == ("1", "1")
                else "non-standard — check by hand"
            )
            print(f"  maker={mk} taker={tk}: {n:>3} series   ({note})")
        print("\n  yaml block for data/fee_schedule.yaml:")
        print("  series:")
        for ticker, name, mk, tk in rows:
            clean = " ".join(name.split()).replace('"', "'")
            print(f"    {ticker}: {{maker: {mk}, taker: {tk}}}  # {clean}")

    print("\n" + "-" * 70)
    print("NEXT STEP — update data/fee_schedule.yaml:")
    print("  * check formula.base_taker_rate / base_maker_rate above")
    print("  * check formula.rounding_increment_dollars matches the stated rule")
    print("  * paste the `series:` block above (it is the whole table)")
    print("  * confirm defaults.taker_multiplier / maker_multiplier")
    print("  * then run:  python scripts/refresh_fee_schedule.py --mark-verified")
    print("-" * 70)


def schedule_problems() -> list[str]:
    """Reasons the schedule is not safe to mark verified.

    Parses the YAML rather than slicing the text. An earlier string-based
    check split on ``"categories:"``, which also matches inside
    ``maker_free_categories:`` — so it inspected the wrong block entirely and
    happily marked a schedule verified while a multiplier was still null. A
    safety guard that silently inspects the wrong thing is worse than none.
    """
    try:
        import yaml
    except ImportError:
        print(
            "! pyyaml not installed; cannot safely verify. "
            "Run this inside the container: "
            "docker compose run --rm tools python scripts/refresh_fee_schedule.py",
            file=sys.stderr,
        )
        sys.exit(1)

    data = yaml.safe_load(SCHEDULE_PATH.read_text(encoding="utf-8")) or {}
    problems: list[str] = []

    formula = data.get("formula") or {}
    for key in ("base_taker_rate", "base_maker_rate", "rounding_increment_dollars"):
        if formula.get(key) is None:
            problems.append(f"formula.{key} is unset")

    defaults = data.get("defaults") or {}
    for key in ("taker_multiplier", "maker_multiplier"):
        if defaults.get(key) is None:
            problems.append(f"defaults.{key} is unset")

    series = data.get("series") or {}
    if not series:
        problems.append("series table is empty — paste the block the script printed")
    for name, value in series.items():
        if not isinstance(value, dict) or value.get("taker") is None:
            problems.append(f"series {name} has no taker multiplier")
        elif value.get("maker") is None:
            problems.append(f"series {name} has no maker multiplier")

    return problems


def mark_verified(revision: str | None) -> None:
    """Stamp today's date into meta.verified_on, if nothing is unresolved."""
    unresolved = schedule_problems()
    if unresolved:
        print(
            f"! refusing to mark verified: {len(unresolved)} problem(s):\n"
            + "".join(f"    - {item}\n" for item in unresolved)
            + "  Fix them first — fail-closed exists for a reason. Marking "
            "this verified would hide the fact that those markets cannot be "
            "priced.",
            file=sys.stderr,
        )
        sys.exit(1)

    text = SCHEDULE_PATH.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()

    text, n = re.subn(
        r"^(\s*verified_on:).*$", rf"\1 {today}", text, count=1, flags=re.M
    )
    if not n:
        print("! could not find `verified_on:` in the schedule file", file=sys.stderr)
        sys.exit(1)

    if revision:
        text = re.sub(
            r"^(\s*schedule_revision:).*$",
            rf'\1 "{revision}"',
            text,
            count=1,
            flags=re.M,
        )

    SCHEDULE_PATH.write_text(text, encoding="utf-8")
    print(f"-> marked verified on {today}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="use a locally saved PDF")
    parser.add_argument(
        "--mark-verified",
        action="store_true",
        help="stamp today's date after you have filled in the multipliers",
    )
    parser.add_argument("--revision", help="schedule revision string to record")
    args = parser.parse_args()

    if args.mark_verified:
        mark_verified(args.revision)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(args.file) if args.file else Path(tmp) / "fees.pdf"

        if args.file:
            if not pdf.is_file():
                print(f"! no such file: {pdf}", file=sys.stderr)
                return 1
        elif not fetch_pdf(pdf):
            return 1

        text = pdf_to_text(pdf)
        if text is None:
            return 1

        analyse(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
