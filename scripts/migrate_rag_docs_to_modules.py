"""One-shot migration: module-shaped RAG documents -> knowledge_modules.

Reads the 14 curated `doc_type='technical'` documents (excluding CET-rules.pdf),
reassembles each body from its chunks, drafts a summary with the LLM, and
writes `knowledge_modules` rows with mode='on_demand'.

Bodies come from `chunks` joined on chunk_index -- NOT from `documents.raw_content`,
which stores a deliberate 500-char preview, nor `documents.content`, which is an
empty string on rows ingested before that column existed.

All 14 land as on_demand: together they are 19,583 chars against a 20,000-char
pinned budget (shared/prompts/knowledge.py PINNED_BUDGET_CHARS), so pinning them
would starve the budget and silently drop modules.

Usage:
    python -m scripts.migrate_rag_docs_to_modules              # dry run, prints everything
    python -m scripts.migrate_rag_docs_to_modules --write      # insert into knowledge_modules
    python -m scripts.migrate_rag_docs_to_modules --delete-source  # after verifying, drop the 14 docs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List

EXCLUDED_TITLES = {"CET-rules.pdf"}

# Explicit slug/title per source document. Auto-generated titles embed body text
# and an uploader suffix ("Technical: X <body excerpt>... by <Name>"), so a regex
# would be guesswork -- all 14 were reviewed by hand instead.
CURATED: Dict[str, Dict[str, str]] = {
    "NXT Grid Power Plant Smoke Detector Battery": {
        "slug": "smoke-detector-battery",
        "title": "Smoke Detector Battery Specification",
    },
    "Technical: Pre-installation Voltage Matching for BYD/Pylontech Battery Modules Before... by Vaibhav Vaidya": {
        "slug": "battery-module-voltage-matching",
        "title": "Pre-installation Voltage Matching for BYD/Pylontech Battery Modules",
    },
    "Technical: Fuses vs. Breakers on the DC Side For... by Vaibhav Vaidya": {
        "slug": "dc-side-fuses-vs-breakers",
        "title": "Fuses vs. Breakers on the DC Side",
    },
    "Technical: High Current Wiring Requirements Requirement Brazing is mandatory... by Vaibhav Vaidya": {
        "slug": "high-current-wiring-requirements",
        "title": "High Current Wiring Requirements",
    },
    "Technical: Calin Meter Token Validity Top-Up or other tokens... by Vaibhav Vaidya": {
        "slug": "calin-meter-token-validity",
        "title": "Calin Meter Token Validity",
    },
    "Technical: Solcast Irradiance Data Evaluation Overview We utilize Solcast... by Vaibhav Vaidya": {
        "slug": "solcast-irradiance-evaluation",
        "title": "Solcast Irradiance Data Evaluation",
    },
    "Technical: Victron Quattro 15kVA Inverter Operating Power Limits Summary... by Vaibhav Vaidya": {
        "slug": "victron-quattro-15kva-power-limits",
        "title": "Victron Quattro 15kVA Inverter Operating Power Limits",
    },
    "Technical: Azimuth Calculation Azimuth is defined as the angle... by Vaibhav Vaidya": {
        "slug": "azimuth-calculation",
        "title": "Azimuth Calculation",
    },
    "Guidelines for Sizing PV to MPPT cables": {
        "slug": "pv-to-mppt-cable-sizing",
        "title": "Guidelines for Sizing PV to MPPT Cables",
    },
    "IEC Recommendations for trench depth and demarcation": {
        "slug": "iec-trench-depth-demarcation",
        "title": "IEC Recommendations for Trench Depth and Demarcation",
    },
    "Decoding Victron Inverter Quattro LED error codes": {
        "slug": "victron-quattro-led-codes",
        "title": "Decoding Victron Quattro Inverter LED Error Codes",
    },
    "Decoding Pylontech Battery LED error codes": {
        "slug": "pylontech-led-codes",
        "title": "Decoding Pylontech Battery LED Error Codes",
    },
    "Decoding BYD Battery BMS and BMU LED error codes": {
        "slug": "byd-bms-bmu-led-codes",
        "title": "Decoding BYD Battery BMS and BMU LED Error Codes",
    },
    "BYD LV Flex Module large scale failure event debug flow": {
        "slug": "byd-lv-flex-failure-debug-flow",
        "title": "BYD LV Flex Module Large-Scale Failure Debug Flow",
    },
}


def is_migration_candidate(doc: Dict[str, Any]) -> bool:
    """Technical documents only, minus the genuine RAG corpus (CET-rules.pdf)."""
    doc_type = (doc.get("metadata") or {}).get("doc_type")
    return doc_type == "technical" and doc.get("title") not in EXCLUDED_TITLES


def assemble_body(chunks: List[Dict[str, Any]]) -> str:
    """Full document text, rebuilt from its chunks in index order."""
    if not chunks:
        raise ValueError("cannot assemble a body from no chunks")
    ordered = sorted(chunks, key=lambda c: c["chunk_index"])
    return "\n\n".join(c["content"] for c in ordered)


def build_module_row(doc: Dict[str, Any], body: str, summary: str) -> Dict[str, Any]:
    """A knowledge_modules row for one migrated document."""
    curated = CURATED[doc["title"]]
    return {
        "slug": curated["slug"],
        "title": curated["title"],
        "summary": summary,
        "body": body,
        "tags": [],
        "scope": "sector",
        "mode": "on_demand",
        "source": "ingested",
        "source_ref": doc["id"],
        "updated_by": "migration:rag-docs-to-modules",
    }


SUMMARY_PROMPT = (
    "Write a single sentence, at most 20 words, describing what this technical "
    "reference covers. It is shown to an AI assistant as the only basis for "
    "deciding whether to fetch the full document, so name the specific equipment, "
    "standard or calculation involved. Reply with the sentence only.\n\n"
    "Title: {title}\n\nContent:\n{body}"
)


async def draft_summary(title: str, body: str) -> str:
    """LLM-drafted catalog line; falls back to the first sentence."""
    try:
        from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        gateway = get_default_generation_gateway(default_model=model)
        response = await gateway.generate(
            [LLMMessage(role="user", text=SUMMARY_PROMPT.format(title=title, body=body[:4000]))],
            GenerationOptions(model=model, temperature=0.2, max_output_tokens=100),
        )
        text = (response.text or "").strip()
        if text:
            return text
    except Exception as e:  # noqa: BLE001 -- a summary is not worth failing the migration over
        print(f"  ! summary generation failed for {title!r}: {e}")

    first = body.strip().split("\n\n")[0].strip().lstrip("#").strip()
    sentence = first.split(". ")[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def _client():
    from dotenv import load_dotenv

    load_dotenv("chat_orchestrator/.env")
    from supabase import create_client

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")
    if not (url and key):
        raise SystemExit("CHAT_DB_URL / CHAT_DB_SERVICE_KEY not set")
    return create_client(url, key)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="insert rows into knowledge_modules")
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="delete the migrated documents from the RAG tables (run only after verifying --write)",
    )
    args = parser.parse_args()
    if args.write and args.delete_source:
        raise SystemExit(
            "--write and --delete-source are mutually exclusive; run them as separate invocations."
        )

    client = _client()
    docs = (
        client.table("documents")
        .select("id, title, metadata")
        .order("ingested_at", desc=True)
        .execute()
        .data
        or []
    )
    candidates = [d for d in docs if is_migration_candidate(d)]
    print(f"Found {len(candidates)} migration candidates (expected 14)")
    if len(candidates) != 14:
        raise SystemExit(
            f"Expected exactly 14 candidates, found {len(candidates)}. "
            "Refusing to proceed -- reconcile CURATED against the database first."
        )

    rows = []
    for doc in candidates:
        chunks = (
            client.table("chunks")
            .select("chunk_index, content")
            .eq("document_id", doc["id"])
            .order("chunk_index")
            .execute()
            .data
            or []
        )
        body = assemble_body(chunks)
        summary = await draft_summary(CURATED[doc["title"]]["title"], body)
        row = build_module_row(doc, body=body, summary=summary)
        rows.append(row)
        print(f"\n--- {row['slug']} ({len(body)} chars, {len(chunks)} chunks) ---")
        print(f"  title:   {row['title']}")
        print(f"  summary: {row['summary']}")

    total = sum(len(r["body"]) for r in rows)
    print(f"\nTotal body chars: {total} (pinned budget is 20000; all rows are on_demand)")

    if args.delete_source:
        ids = [r["source_ref"] for r in rows]
        for doc_id in ids:
            client.table("documents").delete().eq("id", doc_id).execute()
        print(f"\nDeleted {len(ids)} source documents (chunks/entity_mentions cascade)")
        return

    if not args.write:
        print("\nDry run. Re-run with --write to insert.")
        with open("migration_preview.json", "w") as f:
            json.dump(rows, f, indent=2)
        print("Preview written to migration_preview.json")
        return

    client.table("knowledge_modules").insert(rows).execute()
    print(f"\nInserted {len(rows)} knowledge_modules rows")


if __name__ == "__main__":
    asyncio.run(main())
