"""Create the singleton provider-backed context modules. Idempotent.

Both are created pinned to no prompts. Attaching them is a deliberate
operator action in the Context admin page -- seeding must never silently
change what any prompt renders.

Usage:
    python scripts/seed_context_provider_modules.py            # dry run
    python scripts/seed_context_provider_modules.py --apply
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Set

SEED_MODULES: List[Dict[str, Any]] = [
    {
        "slug": "directory",
        "title": "Known Grids, Organizations and People",
        "summary": (
            "The grids, organizations and team members this caller may see. "
            "Use to disambiguate a name mentioned in a message."
        ),
        "body": None,
        "tags": ["directory"],
        "scope": "sector",
        "mode": "pinned",
        "source": "directory",
        "source_ref": None,
    },
    {
        "slug": "entity-graph",
        "title": "Knowledge Graph Overview",
        "summary": (
            "Entity types, relationship types and example entities in the knowledge "
            "graph. Use to decide what to search for before querying the graph."
        ),
        "body": None,
        "tags": ["graph"],
        "scope": "sector",
        "mode": "pinned",
        "source": "graph",
        "source_ref": None,
    },
]


def rows_to_insert(existing_slugs: Set[str]) -> List[Dict[str, Any]]:
    """The seed rows not already present. Never updates an existing row."""
    return [m for m in SEED_MODULES if m["slug"] not in existing_slugs]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    from supabase import create_client

    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        print("CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set", file=sys.stderr)
        return 1

    client = create_client(url, key)
    existing = {
        row["slug"]
        for row in (client.table("knowledge_modules").select("slug").execute().data or [])
    }
    rows = rows_to_insert(existing)

    if not rows:
        print("Nothing to seed; both modules already exist.")
        return 0

    for row in rows:
        print(f"  + {row['slug']} (source={row['source']}, mode={row['mode']})")

    if not args.apply:
        print(f"\nDry run. {len(rows)} module(s) would be created. Re-run with --apply.")
        return 0

    client.table("knowledge_modules").insert(rows).execute()
    print(f"\nCreated {len(rows)} module(s). Attach them to prompts in the Context page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
