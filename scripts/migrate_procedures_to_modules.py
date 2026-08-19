"""Split the procedures out of troubleshooting.procedures into context modules.

Dry run by default. Reuses ProcedureProvider._parse_procedures so the
migration and the ingestion flow can never drift on what counts as a
procedure.

NOTE on source: this plan's own investigation notes said the procedures live
inside customer.system's Google Doc. Verified against live production on
2026-08-19, right before this script was written, and that was stale --
customer.system's live body (DB and Doc alike) has zero '## Procedure N'
headers. All 25 procedures are in the troubleshooting.procedures doc
instead, and that doc is already being appended onto system_instructions
every turn by prepare_context.py's _fetch_troubleshooting(), uncapped (
MAX_CONTEXT_CHARS/_cap_context only guard the context channel, never
system_instructions) -- worse than the mid-document-truncation risk this
plan was originally written to guard against. See the
procedures-live-in-troubleshooting-doc-not-customer-system memory for the
full measurement. This script reads troubleshooting.procedures throughout.

Usage:
    python scripts/migrate_procedures_to_modules.py            # dry run
    python scripts/migrate_procedures_to_modules.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from typing import Any, Dict, List

SOURCE_PROMPT_ID = "troubleshooting.procedures"

SLUG_PREFIX = "procedure-"

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_APOSTROPHE = re.compile(r"['’]")

MAX_BODY_CHARS_FOR_PROMPT = 4000


def slug_for_title(title: str) -> str:
    """A stable address for a procedure, derived from its title.

    Deliberately not derived from the procedure number: numbering in the
    Doc is editorial and will change, while prompt pins reference the slug.
    """
    cleaned = _APOSTROPHE.sub("", (title or "").strip().lower())
    body = _NON_SLUG.sub("-", cleaned).strip("-")
    if not body:
        raise ValueError("cannot derive a slug from an empty title")
    return f"{SLUG_PREFIX}{body}"


def procedure_to_module(procedure: Any, summary: str = "") -> Dict[str, Any]:
    """Map one parsed Procedure to a knowledge_modules row.

    `summary` overrides the procedure's ### Purpose text -- the migration
    generates a symptom-first line and a human reviews it, because an
    on_demand module is selected from its summary alone.
    """
    resolved = (summary or getattr(procedure, "purpose", "") or "").strip()
    if not resolved:
        raise ValueError(
            f"procedure '{procedure.title}' has no ### Purpose and no generated "
            f"summary; an on_demand module without a summary is invisible to the model"
        )
    return {
        "slug": slug_for_title(procedure.title),
        "title": procedure.title.strip(),
        "summary": resolved,
        "body": procedure.full_text.strip(),
        "tags": ["procedure", "troubleshooting"],
        "scope": "sector",
        "mode": "on_demand",
        "source": "manual",
    }


def detect_slug_collisions(
    modules: List[Dict[str, Any]], existing_slugs: set
) -> List[str]:
    """Human-readable collision reports. Empty means safe to apply."""
    problems: List[str] = []
    counts = Counter(m["slug"] for m in modules)

    for slug, count in sorted(counts.items()):
        if count > 1:
            titles = ", ".join(repr(m["title"]) for m in modules if m["slug"] == slug)
            problems.append(f"{count} procedures share the slug '{slug}': {titles}")

    for slug in sorted(set(counts) & existing_slugs):
        problems.append(f"slug '{slug}' already exists in knowledge_modules")

    return problems


def truncate_body_for_prompt(body: str) -> str:
    """Cap the body sent to the summariser -- the opening is what matters."""
    if len(body) <= MAX_BODY_CHARS_FOR_PROMPT:
        return body
    return body[:MAX_BODY_CHARS_FOR_PROMPT]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="use each procedure's ### Purpose verbatim instead of generating summaries",
    )
    args = parser.parse_args()

    from supabase import create_client

    from shared.config.db_credentials import chat_db_service_key, chat_db_url
    from shared.prompts import PROMPTS

    # Provenance first: an operator must know whether they are reading the
    # live Doc or a bundled placeholder before trusting anything below.
    body, source, version = PROMPTS.resolve(SOURCE_PROMPT_ID)
    print(
        f"{SOURCE_PROMPT_ID} resolved from {source.value} (version={version}), "
        f"{len(body)} chars"
    )

    from orchestrator.services.procedure_provider import ProcedureProvider

    procedures = ProcedureProvider()._parse_procedures(PROMPTS.text(SOURCE_PROMPT_ID))
    if not procedures:
        print(
            f"No '## Procedure N: Title' headers found. If you expected some, "
            f"{SOURCE_PROMPT_ID} is resolving to the bundled file rather than the "
            f"live Google Doc -- check the provenance line above.",
            file=sys.stderr,
        )
        return 1

    print(f"\nParsed {len(procedures)} procedure(s):\n")

    modules: List[Dict[str, Any]] = []
    # Accumulate rather than stop at the first bad procedure: a human is
    # about to review this whole list anyway, and finding out about problem
    # #2 only after fixing #1 and re-running means a slower, more annoying
    # loop for no benefit -- nothing partial gets written either way.
    errors: List[str] = []
    for procedure in procedures:
        summary = ""
        if not args.no_llm:
            summary = _generate_summary(procedure)
        try:
            modules.append(procedure_to_module(procedure, summary=summary))
        except ValueError as e:
            errors.append(str(e))

    if errors:
        print(f"\n{len(errors)} procedure(s) cannot be migrated yet:", file=sys.stderr)
        for e in errors:
            print(f"  ! {e}", file=sys.stderr)
        return 1

    client = create_client(chat_db_url(), chat_db_service_key())
    existing = {
        row["slug"]
        for row in (client.table("knowledge_modules").select("slug").execute().data or [])
    }

    collisions = detect_slug_collisions(modules, existing)
    if collisions:
        print("\nRefusing to migrate:", file=sys.stderr)
        for problem in collisions:
            print(f"  ! {problem}", file=sys.stderr)
        return 1

    total_body = 0
    for module in modules:
        total_body += len(module["body"])
        print(f"  {module['slug']}")
        print(f"    title:   {module['title']}")
        print(f"    summary: {module['summary']}")
        print(f"    body:    {len(module['body'])} chars\n")

    print(f"Total procedure text: {total_body} chars")
    print(f"{SOURCE_PROMPT_ID} is currently {len(body)} chars, appended uncapped onto "
          f"system_instructions every turn (see this file's module docstring)")

    if not args.apply:
        print(
            "\nDry run. Review every summary above -- it is the only thing the model "
            "sees when deciding to fetch a procedure. Re-run with --apply to write."
        )
        return 0

    client.table("knowledge_modules").insert(modules).execute()
    print(f"\nCreated {len(modules)} module(s), pinned to no prompts.")
    print(
        "Next: attach them to customer.system and staff.system in the Context page, "
        "confirm in production that get_knowledge_module is being called, and only "
        "then remove the procedures from the Google Doc."
    )
    return 0


def _generate_summary(procedure: Any) -> str:
    """One symptom-first line. Falls back to ### Purpose on any failure."""
    import asyncio

    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
    from shared.llm.model_tiers import resolve_model
    from shared.prompts import PROMPTS

    try:
        prompt = PROMPTS.text(
            "procedure.module_summary",
            title=procedure.title,
            purpose=procedure.purpose or "(none given)",
            body=truncate_body_for_prompt(procedure.full_text),
        )
        gateway = get_default_generation_gateway()
        model = resolve_model(PROMPTS.spec("procedure.module_summary").model)
        response = asyncio.run(
            gateway.generate(
                [LLMMessage(role="user", text=prompt)], GenerationOptions(model=model)
            )
        )
        return (getattr(response, "text", "") or "").strip()
    except Exception as e:
        print(f"    (summary generation failed for '{procedure.title}': {e})", file=sys.stderr)
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
