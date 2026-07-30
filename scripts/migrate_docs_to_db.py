"""One-time migration: seed the DB override layer with today's live Google Doc
content, for every Doc-bound overridable prompt.

Without this, the DB starts empty and every Doc-bound prompt keeps resolving
from its Google Doc exactly as before (that path is unaffected either way).
Running this once makes the DB the active source instead, holding a version
that is byte-identical to what was live at migration time -- so switching to
the Prompts admin page loses nothing already in production.

Publishes directly via OverrideStore rather than through PromptLibrary.publish
(which refuses non-UI callers): this script runs below the UI, with direct
database access, the same trust level as a raw SQL migration -- not through
the access-controlled write API meant for the admin app and automated agents.

Usage:
    python -m scripts.migrate_docs_to_db --actor ops@example.com
    python -m scripts.migrate_docs_to_db --actor ops@example.com --dry-run
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List

from shared.prompts.gdoc import LEGACY_DOC_ENV_VARS
from shared.prompts.overrides import OverrideStore
from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

# Every prompt with a legacy env-var doc binding is, today, also overridable
# (verified against shared/prompts/library/*.prompt frontmatter) -- this is
# exactly the set worth migrating.
DOC_BOUND_PROMPT_IDS: List[str] = list(LEGACY_DOC_ENV_VARS)


def migrate(actor: str, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Propose + publish today's live doc content for each doc-bound prompt.

    Returns one result dict per prompt id with a 'status':
    skipped_no_doc | empty_doc | fetch_failed | published | would_publish
    """
    import os

    store = OverrideStore.from_env()
    if not store.is_configured():
        print(
            "Prompt override storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY); "
            "nothing migrated.",
            file=sys.stderr,
        )
        sys.exit(1)

    results: List[Dict[str, Any]] = []
    for prompt_id in DOC_BOUND_PROMPT_IDS:
        env_var = LEGACY_DOC_ENV_VARS[prompt_id]
        doc_id = os.getenv(env_var, "").strip()
        if not doc_id:
            results.append({"prompt_id": prompt_id, "status": "skipped_no_doc"})
            continue

        try:
            body = fetch_google_doc_markdown(doc_id)
        except Exception as e:
            results.append({"prompt_id": prompt_id, "status": "fetch_failed", "error": str(e)})
            continue

        if not body or not body.strip():
            results.append({"prompt_id": prompt_id, "status": "empty_doc"})
            continue

        if dry_run:
            results.append({"prompt_id": prompt_id, "status": "would_publish", "doc_id": doc_id})
            continue

        version = store.propose(
            prompt_id,
            body,
            note=f"Migrated from live Google Doc {doc_id}",
            actor=actor,
            via="api",
        )
        store.publish(prompt_id, version, actor=actor)
        results.append({"prompt_id": prompt_id, "status": "published", "version": version})

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True, help="Attributed as created_by / updated_by.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would happen; write nothing."
    )
    args = parser.parse_args()

    results = migrate(actor=args.actor, dry_run=args.dry_run)
    for result in results:
        print(f"{result['prompt_id']}: {result['status']}" + (
            f" (v{result['version']})" if "version" in result else ""
        ) + (f" -- {result['error']}" if "error" in result else ""))


if __name__ == "__main__":
    main()
