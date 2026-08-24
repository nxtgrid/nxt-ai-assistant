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
DOC_BINDING_TTL_SECONDS = 60
PRODUCTION = "production"


class OverrideStore:
    """Reads and writes prompt_versions / prompt_labels / prompt_doc_bindings."""

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._label_cache: Optional[Dict[str, int]] = None
        self._label_expires: float = 0.0
        self._body_cache: Dict[Tuple[str, int], str] = {}
        self._doc_binding_cache: Optional[Dict[str, Tuple[str, bool]]] = None
        self._doc_binding_expires: float = 0.0
        self._model_override_cache: Optional[Dict[str, str]] = None
        self._model_override_expires: float = 0.0

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
            LOGGER.opt(exception=True).warning("Could not build the prompt override client")
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
        self._doc_binding_cache = None
        self._doc_binding_expires = 0.0
        self._model_override_cache = None
        self._model_override_expires = 0.0

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
            LOGGER.opt(exception=True).warning("Prompt label fetch failed; using bundled prompts")
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
            LOGGER.opt(exception=True).warning(
                f"Prompt body fetch failed for '{prompt_id}' v{version}; using bundled",
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

    def _doc_bindings(self) -> Dict[str, Tuple[str, bool]]:
        """Every prompt_id -> (doc_id, is_override) binding, one query for all of
        them, cached for DOC_BINDING_TTL_SECONDS. Backs doc_id_for,
        doc_override_for and all_doc_bindings so a per-prompt lookup and the
        Prompts list page both hit the same cached batch instead of one query
        per prompt.
        """
        if self._doc_binding_cache is not None and time.time() < self._doc_binding_expires:
            return self._doc_binding_cache
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_doc_bindings")
                .select("prompt_id, doc_id, is_override")
                .execute()
            )
            self._doc_binding_cache = {
                row["prompt_id"]: (str(row["doc_id"]), bool(row.get("is_override", False)))
                for row in (result.data or [])
            }
        except Exception:
            LOGGER.opt(exception=True).warning(
                "Doc binding fetch failed; using legacy env vars only"
            )
            self._doc_binding_cache = {}
        self._doc_binding_expires = time.time() + DOC_BINDING_TTL_SECONDS
        return self._doc_binding_cache

    def doc_id_for(self, prompt_id: str) -> Optional[str]:
        """Attached doc id for this prompt, falling back to the legacy env var."""
        binding = self._doc_bindings().get(prompt_id)
        if binding is not None:
            return binding[0]
        return legacy_doc_id_for(prompt_id)

    def doc_override_for(self, prompt_id: str) -> bool:
        """Whether this prompt's doc binding should outrank a live DB version.

        False (including when unconfigured or no binding row exists) means
        today's resolution order: DB, then doc, then bundled -- byte-identical
        to before this method existed. There is no env-var equivalent of
        "override": a prompt with only a legacy env var and no binding row
        always resolves as non-override.
        """
        binding = self._doc_bindings().get(prompt_id)
        return binding[1] if binding is not None else False

    def all_doc_bindings(self) -> Dict[str, Tuple[str, bool]]:
        """Every prompt_id -> (doc_id, is_override) binding.

        For the Prompts list page: one call covers every row, rather than
        each row triggering its own doc_id_for/doc_override_for query.
        """
        return dict(self._doc_bindings())

    def _model_overrides(self) -> Dict[str, str]:
        """Every prompt_id -> tier override, one query for all of them,
        cached for DOC_BINDING_TTL_SECONDS -- same shape as _doc_bindings()."""
        if (
            self._model_override_cache is not None
            and time.time() < self._model_override_expires
        ):
            return self._model_override_cache
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_model_overrides")
                .select("prompt_id, tier")
                .execute()
            )
            self._model_override_cache = {
                row["prompt_id"]: row["tier"] for row in (result.data or [])
            }
        except Exception:
            LOGGER.opt(exception=True).warning(
                "Model override fetch failed; using bundled tiers only"
            )
            self._model_override_cache = {}
        self._model_override_expires = time.time() + DOC_BINDING_TTL_SECONDS
        return self._model_override_cache

    def model_tier_for(self, prompt_id: str) -> Optional[str]:
        """This prompt's live tier override, or None if it's on the
        frontmatter default."""
        return self._model_overrides().get(prompt_id)

    def all_model_overrides(self) -> Dict[str, str]:
        """Every prompt_id -> tier override. For the Prompts list page,
        same rationale as all_doc_bindings()."""
        return dict(self._model_overrides())

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

    def set_doc_binding(self, prompt_id: str, doc_id: str, is_override: bool, actor: str) -> None:
        """Attach (or update) this prompt's Google Doc binding.

        Upserts on prompt_id, the table's primary key -- a second call for
        the same prompt updates doc_id/is_override in place rather than
        erroring on a duplicate key.
        """
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_doc_bindings").upsert(
            {"prompt_id": prompt_id, "doc_id": doc_id, "is_override": is_override}
        ).execute()
        self.invalidate()
        LOGGER.info(
            f"Set doc binding for {prompt_id} -> {doc_id} (override={is_override}) by {actor}"
        )

    def clear_doc_binding(self, prompt_id: str, actor: str) -> None:
        """Detach this prompt's doc binding entirely, falling back to the
        legacy env var (if any) on the next resolution.

        Distinct from setting an empty doc_id: this deletes the row so
        doc_id_for/doc_override_for fall through to legacy_doc_id_for/False,
        rather than leaving a row with a blank doc_id that shadows the env
        var with nothing to fetch.
        """
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_doc_bindings").delete().eq("prompt_id", prompt_id).execute()
        self.invalidate()
        LOGGER.info(f"Cleared doc binding for {prompt_id} by {actor}")

    def set_model_override(self, prompt_id: str, tier: str, actor: str) -> None:
        """Set (or update) this prompt's live model tier.

        Upserts on prompt_id, the table's primary key -- same pattern as
        set_doc_binding.
        """
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_model_overrides").upsert(
            {"prompt_id": prompt_id, "tier": tier, "updated_by": actor}
        ).execute()
        self.invalidate()
        LOGGER.info(f"Set model tier for {prompt_id} -> {tier} by {actor}")

    def clear_model_override(self, prompt_id: str, actor: str) -> None:
        """Revert this prompt's tier to its frontmatter default."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_model_overrides").delete().eq("prompt_id", prompt_id).execute()
        self.invalidate()
        LOGGER.info(f"Reverted {prompt_id}'s model tier to bundled default by {actor}")
