"""DB-backed prompt overrides: append-only versions, label-based publishing.

Two-level cache so a save in the admin app becomes visible to the bot without a
restart and without a query per render:

* the label map (prompt_id -> live version) is small and refreshed on a short
  TTL;
* bodies are content-addressed by (prompt_id, version) and never expire.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared.config.db_credentials import chat_db_service_key, chat_db_url
from shared.prompts.gdoc import legacy_doc_id_for
from shared.prompts.spec import body_checksum
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LABEL_TTL_SECONDS = 60
PRODUCTION = "production"


class OverrideStore:
    """Reads and writes prompt_versions / prompt_labels / prompt_doc_bindings."""

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._label_cache: Optional[Dict[str, int]] = None
        self._label_expires: float = 0.0
        self._body_cache: Dict[Tuple[str, int], str] = {}

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "OverrideStore":
        url, key = chat_db_url(), chat_db_service_key()
        if not (url and key):
            LOGGER.info("Prompt override store not configured; bundled prompts only")
            return cls(client=None)
        try:
            from supabase import create_client

            return cls(client=create_client(url, key))
        except Exception:
            LOGGER.warning("Could not build the prompt override client", exc_info=True)
            return cls(client=None)

    def is_configured(self) -> bool:
        return self._client is not None

    # ── pure helpers (unit-tested directly) ──────────────────────────────────
    @staticmethod
    def _next_version(rows: List[Dict[str, Any]]) -> int:
        return max((int(r["version"]) for r in rows), default=0) + 1

    @staticmethod
    def _label_map(rows: List[Dict[str, Any]]) -> Dict[str, int]:
        return {r["prompt_id"]: int(r["version"]) for r in rows if r.get("label") == PRODUCTION}

    # ── reads ────────────────────────────────────────────────────────────────
    def invalidate(self) -> None:
        self._label_cache = None
        self._label_expires = 0.0
        self._body_cache.clear()

    def _labels(self) -> Dict[str, int]:
        if self._label_cache is not None and time.time() < self._label_expires:
            return self._label_cache
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_labels")
                .select("prompt_id, label, version")
                .eq("label", PRODUCTION)
                .execute()
            )
            self._label_cache = self._label_map(result.data or [])
        except Exception:
            LOGGER.warning("Prompt label fetch failed; using bundled prompts", exc_info=True)
            self._label_cache = {}
        self._label_expires = time.time() + LABEL_TTL_SECONDS
        return self._label_cache

    def body_for(self, prompt_id: str) -> Optional[Tuple[str, int]]:
        """The live body and version for this prompt, or None."""
        version = self._labels().get(prompt_id)
        if version is None:
            return None

        cached = self._body_cache.get((prompt_id, version))
        if cached is not None:
            return cached, version

        try:
            result = (
                self._client.table("prompt_versions")
                .select("body")
                .eq("prompt_id", prompt_id)
                .eq("version", version)
                .single()
                .execute()
            )
        except Exception:
            LOGGER.warning(
                f"Prompt body fetch failed for '{prompt_id}' v{version}; using bundled",
                exc_info=True,
            )
            return None

        rows = result.data if isinstance(result.data, list) else ([result.data] if result.data else [])
        if not rows:
            return None
        body = rows[0]["body"]
        self._body_cache[(prompt_id, version)] = body
        return body, version

    def versions(self, prompt_id: str) -> List[Dict[str, Any]]:
        if not self._client:
            return []
        result = (
            self._client.table("prompt_versions")
            .select("version, note, created_at, created_by, created_via, checksum")
            .eq("prompt_id", prompt_id)
            .order("version", desc=True)
            .execute()
        )
        return result.data or []

    def doc_id_for(self, prompt_id: str) -> Optional[str]:
        """Attached doc id for this prompt, falling back to the legacy env var."""
        if self._client:
            try:
                result = (
                    self._client.table("prompt_doc_bindings")
                    .select("doc_id")
                    .eq("prompt_id", prompt_id)
                    .execute()
                )
                if result.data:
                    return str(result.data[0]["doc_id"])
            except Exception:
                LOGGER.warning(
                    f"Doc binding lookup failed for '{prompt_id}'; using the legacy env var",
                    exc_info=True,
                )
        return legacy_doc_id_for(prompt_id)

    # ── writes ───────────────────────────────────────────────────────────────
    def propose(self, prompt_id: str, body: str, note: str, actor: str, via: str = "ui") -> int:
        """Append a new version. Does NOT make it live."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        existing = (
            self._client.table("prompt_versions")
            .select("version")
            .eq("prompt_id", prompt_id)
            .execute()
        )
        version = self._next_version(existing.data or [])
        self._client.table("prompt_versions").insert(
            {
                "prompt_id": prompt_id,
                "version": version,
                "body": body,
                "checksum": body_checksum(body),
                "note": note,
                "created_by": actor,
                "created_via": via,
            }
        ).execute()
        LOGGER.info(f"Proposed {prompt_id} v{version} by {actor} via {via}")
        return version

    def publish(self, prompt_id: str, version: int, actor: str) -> None:
        """Point the production label at a version, making it live."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_labels").upsert(
            {
                "prompt_id": prompt_id,
                "label": PRODUCTION,
                "version": version,
                "updated_by": actor,
            }
        ).execute()
        self.invalidate()
        LOGGER.info(f"Published {prompt_id} v{version} by {actor}")

    def revert_to_default(self, prompt_id: str, actor: str) -> None:
        """Drop the label so resolution falls back to the bundled file."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_labels").delete().eq("prompt_id", prompt_id).eq(
            "label", PRODUCTION
        ).execute()
        self.invalidate()
        LOGGER.info(f"Reverted {prompt_id} to the bundled default by {actor}")
