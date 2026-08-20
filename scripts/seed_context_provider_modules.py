"""Create the singleton provider-backed context modules. Idempotent.

Superseded as the only way to do this: the Context admin page now calls
KnowledgeStore.ensure_singleton_modules on every load (see
nicegui_app/pages/knowledge_modules.py), so opening /knowledge-modules once
has the same effect as running this with --apply. This script now shares
that exact row-shape (shared.prompts.knowledge.SINGLETON_SOURCES) rather than
keeping its own copy, and stays around as a CLI-only alternative -- useful
before the admin page has ever been opened, e.g. scripted into a deployment.

All created pinned to no prompt. Attaching one is a deliberate operator
action in the Context admin page -- seeding must never silently change what
any prompt renders.

Usage:
    python scripts/seed_context_provider_modules.py            # dry run
    python scripts/seed_context_provider_modules.py --apply
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Set


def sources_to_seed(existing_sources: Set[str]) -> List[str]:
    """Singleton sources not already present. Never updates an existing row."""
    from shared.prompts.knowledge import SINGLETON_SOURCES

    return [s for s in SINGLETON_SOURCES if s not in existing_sources]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    from shared.prompts.knowledge import KnowledgeStore

    store = KnowledgeStore.from_env()
    if not store._client:  # noqa: SLF001 -- readiness check, mirrors the admin page
        print("CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set", file=sys.stderr)
        return 1

    existing = {m.source for m in store.all_modules()}
    missing = sources_to_seed(existing)

    if not missing:
        print("Nothing to seed; every singleton module already exists.")
        return 0

    for source in missing:
        print(f"  + {source}")

    if not args.apply:
        print(f"\nDry run. {len(missing)} module(s) would be created. Re-run with --apply.")
        return 0

    results = store.ensure_singleton_modules(actor="seed_context_provider_modules")
    created = [source for source, outcome in results.items() if outcome == "created"]
    failed = {source: outcome for source, outcome in results.items() if outcome not in ("created", "exists")}

    for source in created:
        print(f"  created: {source}")
    for source, outcome in failed.items():
        print(f"  FAILED: {source} -- {outcome}", file=sys.stderr)

    print(f"\nCreated {len(created)} module(s). Attach them to prompts in the Context page.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
