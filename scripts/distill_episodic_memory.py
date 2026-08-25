"""Distil recent chat history per grid / organization into episodic memory.

A hand-run CLI over shared.episodic_memory, which holds the actual logic and
which every image ships. On a schedule this runs as a daemon inside anansi_app
instead -- see anansi_app/scripts/episodic_scheduler.py. This file is not in
any deployed image (no Dockerfile copies repo-root scripts/), so it is only
ever run from a developer machine.

Reuses shared.entity_eligibility for "every eligible grid / organization"
rather than adding a fifth enumeration, the same decision
0013_skill_scheduling.sql made.

Usage:
    PYTHONPATH=.. python scripts/distill_episodic_memory.py --anchor-type grid
    PYTHONPATH=.. python scripts/distill_episodic_memory.py --anchor-type grid --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# Re-exported so anything already importing these from this module -- notably
# shared/tests/test_distill_episodic_memory.py -- keeps working after the move.
from shared.episodic_memory import (  # noqa: F401
    LOOKBACK_DAYS,
    MAX_MESSAGES,
    TARGET_WORDS,
    anchors_to_refresh,
    build_distillation_prompt,
    distill_anchor_type,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-type", choices=["grid", "organization"], required=True)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    result = asyncio.run(distill_anchor_type(args.anchor_type, apply=args.apply))

    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1

    if not result["enumerated"]:
        # Ambiguous by design: this is either a genuinely empty deployment or
        # an Auth DB that did not answer. Say so rather than "nothing to do".
        print(
            f"No eligible {args.anchor_type}s enumerated. That may mean the Auth DB "
            f"is unreachable rather than that there are none -- check before "
            f"concluding anything from an empty run.",
            file=sys.stderr,
        )
        return 1

    if not result["targets"]:
        print("Nothing to refresh.")
        return 0

    print(f"{len(result['targets'])} anchor(s) to refresh: {', '.join(result['targets'])}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to generate and write.")
        return 0

    print(f"Wrote {result['written']}; skipped {len(result['skipped'])}.")
    for name in result["skipped"]:
        print(f"  {name}: no messages or nothing generated, skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
