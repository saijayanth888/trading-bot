"""One-shot bootstrap for subsystem ownership state files.

The Shark/Wheel isolation system (see ``shared/subsystem_ownership.py``)
needs to know which subsystem opened each currently-held position. On
first run the state files don't exist and ``load_owned()`` returns the
empty set — which is fail-safe (no subsystem will act on any position),
but useless in production. This script seeds the two files from the
live Alpaca account state + Wheel's existing journal.

Logic
-----
1. Wheel-owned set =
     all OCC ticker strings in stocks/wheel/state/positions.json (short
     puts and short calls)
   ∪ all underlying symbols where Wheel currently holds long_shares
     (assignment legs)
2. Shark-owned set =
     every us_equity row on Alpaca whose symbol is NOT in the Wheel set

Run with --dry-run to print the plan without writing. Without --force,
the script refuses to overwrite an existing state file.

Usage
-----
    python -m shared.migrate_ownership_bootstrap [--dry-run] [--force]

Idempotency
-----------
Without --force: aborts if either state file already exists, so a
second run cannot wipe deliberate edits. With --force: overwrites both.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

# Allow `python stocks/shared/migrate_ownership_bootstrap.py` to work
# from any cwd by adding the stocks/ root to sys.path.
_STOCKS_ROOT = Path(__file__).resolve().parent.parent
if str(_STOCKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_STOCKS_ROOT))

from shared import subsystem_ownership as so  # noqa: E402

logger = logging.getLogger(__name__)


def _collect_wheel_owned() -> set[str]:
    """Read Wheel's existing journal and derive the OCC + underlying set."""
    try:
        from wheel.state import load_positions
    except Exception as exc:
        logger.warning("wheel.state import failed (%s) — wheel set empty", exc)
        return set()

    positions = load_positions()
    owned: set[str] = set()
    for p in positions:
        # OCC ticker for active option legs
        if p.contract_symbol:
            owned.add(p.contract_symbol.upper())
        # Underlying symbol for assignment-derived shares
        if p.kind == "long_shares" and p.underlying:
            owned.add(p.underlying.upper())
    return owned


def _collect_alpaca_equities() -> list[dict[str, Any]]:
    """Return today's equity positions on the Alpaca account."""
    try:
        from shark.data.alpaca_data import get_positions
    except Exception as exc:
        logger.error("shark.data.alpaca_data import failed: %s", exc)
        return []

    try:
        all_positions = get_positions()
    except Exception as exc:
        logger.error("get_positions() failed: %s", exc)
        return []

    return [
        p for p in all_positions
        if p.get("asset_class", "us_equity") == "us_equity"
    ]


def plan(wheel_owned: set[str], alpaca_equities: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Compute the two final ownership sets without touching disk."""
    # Wheel owns its journaled OCC tickers + any assigned-share underlyings.
    final_wheel = {s.upper() for s in wheel_owned if s}

    # Shark owns every us_equity on the account NOT claimed by Wheel.
    final_shark: set[str] = set()
    for p in alpaca_equities:
        sym = (p.get("symbol") or "").upper()
        if not sym:
            continue
        if sym in final_wheel:
            continue
        final_shark.add(sym)

    return final_shark, final_wheel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap Shark/Wheel ownership state from Alpaca + Wheel journal.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan, do not write.")
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing state files (default: abort if either exists).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    shark_path = so._state_path("shark")
    wheel_path = so._state_path("wheel")

    if not args.force and not args.dry_run:
        existing = [p for p in (shark_path, wheel_path) if p.exists()]
        if existing:
            logger.error(
                "Refusing to overwrite existing state file(s): %s. "
                "Re-run with --force to overwrite, or --dry-run to inspect.",
                ", ".join(str(p) for p in existing),
            )
            return 2

    logger.info("Reading Wheel journal …")
    wheel_owned = _collect_wheel_owned()
    logger.info("Wheel journal owns %d items: %s",
                len(wheel_owned), sorted(wheel_owned))

    logger.info("Querying Alpaca for equity positions …")
    equities = _collect_alpaca_equities()
    logger.info("Alpaca returned %d equity row(s)", len(equities))

    shark_set, wheel_set = plan(wheel_owned, equities)

    logger.info("Proposed Shark ownership (%d): %s",
                len(shark_set), sorted(shark_set))
    logger.info("Proposed Wheel ownership (%d): %s",
                len(wheel_set), sorted(wheel_set))

    if args.dry_run:
        logger.info("[DRY-RUN] No files written.")
        return 0

    so.save_owned("shark", shark_set)
    so.save_owned("wheel", wheel_set)
    logger.info("Wrote %s", shark_path)
    logger.info("Wrote %s", wheel_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
