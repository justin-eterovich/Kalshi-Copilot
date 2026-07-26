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
    """Report what the PDF says about rates and per-category multipliers."""
    print("\n" + "=" * 70)
    print("FEE SCHEDULE FINDINGS")
    print("=" * 70)

    rates = sorted(set(re.findall(r"0\.0\d{1,3}", text)))
    if rates:
        print(f"\nrate-like constants found: {', '.join(rates)}")
        print("  (the general taker rate has historically been 0.07)")

    revision = re.search(
        r"(?:updated?|revis\w*|effective)[^\n]{0,40}?"
        r"(\d{1,2}[./]\d{1,2}[./]\d{2,4}|[A-Z][a-z]+ \d{1,2},? \d{4})",
        text,
        re.I,
    )
    if revision:
        print(f"\nschedule revision: {revision.group(1)}")

    print("\ncategory mentions:")
    categories = [
        "crypto",
        "bitcoin",
        "ethereum",
        "s&p",
        "nasdaq",
        "sports",
        "weather",
        "climate",
        "economics",
        "politics",
        "elections",
        "financials",
    ]
    hits = 0
    for category in categories:
        for match in re.finditer(re.escape(category), text, re.I):
            start = max(0, match.start() - 90)
            snippet = " ".join(text[start : match.end() + 90].split())
            print(f"  [{category}] …{snippet}…")
            hits += 1
            break
    if not hits:
        print("  (none found — the schedule may use a single uniform rate)")

    print("\n" + "-" * 70)
    print("NEXT STEP — edit data/fee_schedule.yaml by hand:")
    print("  * set `formula.base_taker_rate` if it differs from 0.07")
    print("  * set `formula.maker_rate_fraction` (maker fees as a share of taker)")
    print("  * replace every `null` in `categories:` with its real multiplier")
    print("    (relative to base_taker_rate: 1.0 == the standard rate)")
    print("  * list any maker-free categories in `maker_free_categories`")
    print("  * then run:  python scripts/refresh_fee_schedule.py --mark-verified")
    print("-" * 70)


def unresolved_categories() -> list[str]:
    """Categories whose multiplier is still unknown.

    Parses the YAML rather than slicing the text. An earlier string-based
    check split on ``"categories:"``, which also matches inside
    ``maker_free_categories:`` — so it inspected the wrong block entirely and
    happily marked a schedule verified while ``crypto`` was still null. A
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
    categories = data.get("categories") or {}
    return sorted(name for name, value in categories.items() if value is None)


def mark_verified(revision: str | None) -> None:
    """Stamp today's date into meta.verified_on, if nothing is unresolved."""
    unresolved = unresolved_categories()
    if unresolved:
        print(
            f"! refusing to mark verified: {len(unresolved)} category "
            f"multiplier(s) still null: {', '.join(unresolved)}\n"
            f"  Fill them in first — fail-closed exists for a reason. Marking "
            f"this verified would hide the fact that those markets still "
            f"cannot be priced.",
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
