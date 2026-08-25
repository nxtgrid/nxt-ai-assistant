"""Propose knowledge modules from an existing instructions document.

By default prints JSON for review -- writes nothing, so a human can paste the
reviewed modules into the Knowledge Modules page, which is where the audit
trail lives. Pass --write to insert the proposed modules directly into
knowledge_modules instead (still prints what it inserted).

Usage:
    python -m scripts.seed_knowledge_modules customer.system
    python -m scripts.seed_knowledge_modules customer.system --write --actor ops@example.com
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Dict, List

_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_SITE = re.compile(r"\bsite\s+([A-Za-z0-9_-]+)\b", re.IGNORECASE)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _summary(body: str) -> str:
    first = body.strip().split("\n\n")[0].strip()
    sentence = first.split(". ")[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def propose_modules(text: str) -> List[Dict[str, object]]:
    """One proposed module per '## ' heading with a non-empty body."""
    matches = list(_HEADING.finditer(text))
    modules: List[Dict[str, object]] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if not body:
            continue
        site = _SITE.search(title)
        modules.append(
            {
                "slug": _slug(title),
                "title": title,
                "summary": _summary(body),
                "body": body,
                "tags": [],
                "scope": f"site:{site.group(1).upper()}" if site else "sector",
            }
        )
    return modules


def _write(modules: List[Dict[str, object]], actor: str) -> None:
    from shared.prompts.knowledge import KnowledgeStore

    store = KnowledgeStore.from_env()
    if store._client is None:  # noqa: SLF001 -- readiness check before writing
        print(
            "Knowledge storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY); "
            "nothing written.",
            file=sys.stderr,
        )
        sys.exit(1)

    for module in modules:
        row = dict(module, updated_by=actor)
        store._client.table("knowledge_modules").insert(row).execute()  # noqa: SLF001
        print(f"Inserted {module['slug']!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt_id", nargs="?", default="customer.system")
    parser.add_argument(
        "--write", action="store_true", help="Insert into knowledge_modules instead of printing."
    )
    parser.add_argument("--actor", default="seed_knowledge_modules", help="updated_by for --write.")
    args = parser.parse_args()

    from shared.prompts import PROMPTS

    modules = propose_modules(PROMPTS.text(args.prompt_id))

    if args.write:
        _write(modules, args.actor)
    else:
        print(json.dumps(modules, indent=2))


if __name__ == "__main__":
    main()
