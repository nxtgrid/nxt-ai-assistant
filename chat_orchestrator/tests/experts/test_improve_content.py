"""Tests for the Improve Content step handler (Document Ingestion Expert).

This handler had zero functional coverage before this file: only prompt
checksum/parity tests touched it (test_prompt_parity.py). Covers:

- _build_fallback_title (the module's one pure helper)
- _auto_generate_title's accept-vs-fallback branching
- The quality-eval flow's JSON parsing (valid, markdown-fenced, fail-open)
- The modification and quality-decision resume branches
- Google Drive passthrough (input_mode outside interactive/inline_text)
- Each of the three _call_gemini call sites (naming, modification,
  quality_eval) independently resolving its own prompt's tier via
  resolve_model(PROMPTS.spec(<id>).model) -- the behavior introduced by
  docs/superpowers/plans/2026-08-08-model-tier-selection.md, previously
  unverified by any test.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.experts.step_context import StepContext

# NOT `from orchestrator.experts.handlers.ingestion_expert import improve_content` --
# that package's __init__.py does `from .improve_content import improve_content`,
# which rebinds the `improve_content` attribute on the ingestion_expert
# package to the step *function*, shadowing the submodule of the same name.
# `import a.b.c as x` doesn't escape this either -- it's equivalent to
# `import a.b.c; x = a.b.c`, and that attribute walk hits the same shadowed
# name. importlib.import_module() fetches the submodule straight from
# sys.modules, bypassing the shadowed attribute (see
# test_gtr_analysis_conversation.py's TestRunAnalysisTurnGateway for the same
# pattern against the same __init__.py-shadowing shape).
ic = importlib.import_module(
    "orchestrator.experts.handlers.ingestion_expert.improve_content"
)

_SENTINEL_THINKING_MODEL = "sentinel-thinking-model"
_SENTINEL_FAST_MODEL = "sentinel-fast-model"
_SENTINEL_LITE_MODEL = "sentinel-lite-model"


@pytest.fixture(autouse=True)
def _force_bundled_prompts(monkeypatch):
    """Force PROMPTS to resolve the ingestion.improve_content.* prompts from
    the bundled .prompt files, regardless of what's configured in the
    environment.

    PROMPTS is a process-wide singleton (shared/prompts/core.py) whose DB and
    Google-Doc override lookups activate automatically whenever
    CHAT_DB_URL/GOOGLE_SERVICE_ACCOUNT_JSON happen to be set in the
    environment -- e.g. a developer's chat_orchestrator/.env copied in for
    unrelated local-dev reasons. Without this, these tests would silently
    stop testing the committed bundled prompts and start testing whatever's
    live in the real chat_db prompts table at the moment they happen to run.
    See test_expert_instructions_provider_library.py's fixture of the same
    name for the incident this pattern guards against. Monkeypatch
    auto-reverts after each test.
    """
    monkeypatch.setattr(ic.PROMPTS, "_db_body_for", None)
    monkeypatch.setattr(ic.PROMPTS, "_gdoc_body_for", None)


@pytest.fixture(autouse=True)
def _stub_uploader_lookup(monkeypatch):
    """_finalize_content resolves the uploader's name via a real
    asyncpg.connect() against AUTH_DB_HOST that only fails open after an
    actual connection attempt. Uploader-name resolution isn't in scope for
    this file, so stub it out to keep every test hermetic and fast.
    """
    monkeypatch.setattr(ic, "_lookup_uploader_name", AsyncMock(return_value="Jane Doe"))


@pytest.fixture(autouse=True)
def _tier_model_env(monkeypatch):
    """Every _call_gemini call site resolves its model via
    resolve_model(PROMPTS.spec(id).model), which reads MODEL_THINKING /
    MODEL_FAST / MODEL_LITE straight from the environment and raises
    RuntimeError if the tier's var isn't set (shared/llm/model_tiers.py).
    CI sets these for the Tests job (.github/workflows/ci.yml); a bare local
    shell usually doesn't. Set explicit, distinct sentinel values so
    behavior -- and model-resolution assertions -- don't depend on ambient
    configuration.
    """
    monkeypatch.setenv("MODEL_THINKING", _SENTINEL_THINKING_MODEL)
    monkeypatch.setenv("MODEL_FAST", _SENTINEL_FAST_MODEL)
    monkeypatch.setenv("MODEL_LITE", _SENTINEL_LITE_MODEL)


def _make_context(**overrides) -> StepContext:
    """Create a StepContext with sensible defaults for the improve_content step."""
    defaults = {
        "packet_id": "ingest_test_123",
        "packet_type": "document_ingestion",
        "packet_goal": "Ingest a document into the knowledge base",
        "packet_inputs": {},
        "packet_state": {},
        "current_step": "improve_content",
        "steps_completed": [],
        "user_input": "",
        "user_email": "uploader@example.com",
    }
    defaults.update(overrides)
    return StepContext(**defaults)


def _install_gateway(monkeypatch, *response_texts: str | None):
    """Stub get_default_generation_gateway. Each positional text is returned
    from successive .generate() calls, in call order. Returns
    (factory_mock, gateway_stub) so tests can assert on how the factory was
    constructed and what was passed to .generate().
    """
    responses = [SimpleNamespace(text=t) for t in response_texts]
    mock_gateway = SimpleNamespace(generate=AsyncMock(side_effect=responses))
    factory = MagicMock(return_value=mock_gateway)
    monkeypatch.setattr(ic, "get_default_generation_gateway", factory)
    return factory, mock_gateway


def _forbid_gateway(monkeypatch):
    """Stub get_default_generation_gateway to blow up if called -- for
    branches that must short-circuit before ever touching the LLM.
    """

    def _boom(**kwargs):
        raise AssertionError(
            f"get_default_generation_gateway should not have been called (kwargs={kwargs})"
        )

    monkeypatch.setattr(ic, "get_default_generation_gateway", _boom)


# ---------------------------------------------------------------------------
# _build_fallback_title (the module's one pure, synchronous helper)
# ---------------------------------------------------------------------------


class TestBuildFallbackTitle:
    def test_uses_first_eight_content_words_when_enough_are_available(self):
        content = (
            "This document explains how the inverter firmware update "
            "process works end to end for every deployed site."
        )
        title = ic._build_fallback_title(content, "technical", "Jane Doe")

        assert title == (
            "Technical: This document explains how the inverter firmware "
            "update... by Jane Doe"
        )

    def test_falls_back_to_generic_title_when_content_has_too_few_words(self):
        title = ic._build_fallback_title("Hi there", "faq", "Jane Doe")
        assert title == "Faq Document Submitted by Jane Doe"

    def test_strips_markdown_punctuation_and_skips_single_char_tokens(self):
        content = "**Setup** guide: a b configure the router properly today now"
        title = ic._build_fallback_title(content, "runbook", "A B")

        assert title == (
            "Runbook: Setup guide configure the router properly today now... by A B"
        )

    def test_doc_type_label_replaces_underscores_and_title_cases(self):
        title = ic._build_fallback_title("short", "release_notes", "Jane")
        assert title == "Release Notes Document Submitted by Jane"


# ---------------------------------------------------------------------------
# _auto_generate_title: accept the LLM's title, or fall back to a
# content-derived one when it's too short or missing.
# ---------------------------------------------------------------------------


class TestAutoGenerateTitle:
    @pytest.mark.asyncio
    async def test_returns_llm_title_when_it_has_at_least_five_words(self, monkeypatch):
        monkeypatch.setattr(
            ic, "_call_gemini", AsyncMock(return_value="A Complete Guide To Inverter Setup")
        )

        title = await ic._auto_generate_title("some content", "technical", "Jane Doe")

        assert title == "A Complete Guide To Inverter Setup"

    @pytest.mark.asyncio
    async def test_exactly_five_words_is_accepted_not_treated_as_a_fallback(self, monkeypatch):
        monkeypatch.setattr(ic, "_call_gemini", AsyncMock(return_value="One Two Three Four Five"))

        title = await ic._auto_generate_title("content", "technical", "Jane Doe")

        assert title == "One Two Three Four Five"

    @pytest.mark.asyncio
    async def test_strips_surrounding_quotes_the_model_sometimes_adds(self, monkeypatch):
        monkeypatch.setattr(
            ic, "_call_gemini", AsyncMock(return_value='"A Complete Guide To Setup"')
        )

        title = await ic._auto_generate_title("some content", "technical", "Jane Doe")

        assert title == "A Complete Guide To Setup"

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_title_is_too_short(self, monkeypatch):
        monkeypatch.setattr(ic, "_call_gemini", AsyncMock(return_value="Short Title"))
        content = "Full paragraph of real document content used for the fallback title."

        title = await ic._auto_generate_title(content, "technical", "Jane Doe")

        assert title == ic._build_fallback_title(content, "technical", "Jane Doe")
        assert title != "Short Title"

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_none(self, monkeypatch):
        monkeypatch.setattr(ic, "_call_gemini", AsyncMock(return_value=None))
        content = "Full paragraph of real document content used for the fallback title."

        title = await ic._auto_generate_title(content, "technical", "Jane Doe")

        assert title == ic._build_fallback_title(content, "technical", "Jane Doe")

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(ic, "_call_gemini", AsyncMock(return_value=""))
        content = "Full paragraph of real document content used for the fallback title."

        title = await ic._auto_generate_title(content, "technical", "Jane Doe")

        assert title == ic._build_fallback_title(content, "technical", "Jane Doe")


# ---------------------------------------------------------------------------
# Quality-eval flow (first run): LLM JSON parsing, including fail-open paths
# ---------------------------------------------------------------------------


class TestQualityEvalFlow:
    @pytest.mark.asyncio
    async def test_first_run_without_content_returns_failure(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(packet_state={"input_mode": "interactive"})

        result = await ic.improve_content(ctx)

        assert result.is_success is False
        assert "No document content" in result.error

    @pytest.mark.asyncio
    async def test_valid_json_needs_improvement_presents_a_choice(self, monkeypatch):
        payload = json.dumps(
            {
                "is_good": False,
                "reasoning": "Missing a conclusion.",
                "suggested_version": "Improved version of the content.",
            }
        )
        _install_gateway(monkeypatch, payload)

        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "document_content": "Raw pasted content that needs a conclusion.",
                "detected_doc_type": "technical",
            }
        )

        result = await ic.improve_content(ctx)

        assert result.needs_user_input is True
        assert result.state_updates["suggested_content"] == "Improved version of the content."
        assert result.state_updates["awaiting_quality_decision"] is True
        assert "Missing a conclusion." in result.user_prompt

    @pytest.mark.asyncio
    async def test_markdown_fenced_json_is_parsed(self, monkeypatch):
        payload = (
            "```json\n"
            + json.dumps(
                {
                    "is_good": False,
                    "reasoning": "Too terse.",
                    "suggested_version": "A longer, improved version.",
                }
            )
            + "\n```"
        )
        _install_gateway(monkeypatch, payload)

        ctx = _make_context(
            packet_state={"input_mode": "inline_text", "document_content": "Terse content."}
        )

        result = await ic.improve_content(ctx)

        assert result.state_updates["suggested_content"] == "A longer, improved version."

    @pytest.mark.asyncio
    async def test_good_content_skips_straight_to_finalize(self, monkeypatch):
        payload = json.dumps({"is_good": True, "reasoning": "Looks solid."})
        _install_gateway(monkeypatch, payload, "A Perfectly Fine Generated Title")

        ctx = _make_context(
            packet_state={"input_mode": "interactive", "document_content": "Already-good content."}
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Already-good content."
        assert result.data["document_title"] == "A Perfectly Fine Generated Title"

    @pytest.mark.asyncio
    async def test_invalid_json_fails_open_and_accepts_content_as_is(self, monkeypatch):
        _, gateway = _install_gateway(
            monkeypatch,
            "Sorry, I can't help with that.",  # not JSON at all
            "A Reasonably Long Fallback-Ready Title",
        )

        ctx = _make_context(
            packet_state={"input_mode": "interactive", "document_content": "Some original content."}
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Some original content."
        assert result.data["document_title"] == "A Reasonably Long Fallback-Ready Title"
        assert gateway.generate.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_llm_response_fails_open_and_accepts_content_as_is(self, monkeypatch):
        _, gateway = _install_gateway(monkeypatch, "", "A Second Reasonably Long Title")

        ctx = _make_context(
            packet_state={"input_mode": "interactive", "document_content": "Some original content."}
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Some original content."
        assert gateway.generate.await_count == 2


# ---------------------------------------------------------------------------
# Modification resume branch (awaiting_modification_input)
# ---------------------------------------------------------------------------


class TestModificationFlow:
    @pytest.mark.asyncio
    async def test_cancel_word_cancels_ingestion(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_modification_input": True,
                "document_content": "orig",
            },
            user_input="cancel",
        )

        result = await ic.improve_content(ctx)

        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_llm_returns_new_version_and_prompts_again(self, monkeypatch):
        _install_gateway(monkeypatch, "Here is the revised content with your changes applied.")

        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_modification_input": True,
                "original_user_content": "Original content.",
                "suggested_content": "Previously suggested content.",
                "quality_iteration_count": 0,
                "detected_doc_type": "technical",
            },
            user_input="Make it more formal",
        )

        result = await ic.improve_content(ctx)

        assert result.needs_user_input is True
        assert (
            result.state_updates["suggested_content"]
            == "Here is the revised content with your changes applied."
        )
        assert result.state_updates["quality_iteration_count"] == 1
        assert result.state_updates["awaiting_modification_input"] is False
        assert result.state_updates["awaiting_quality_decision"] is True

    @pytest.mark.asyncio
    async def test_llm_failure_accepts_current_suggestion(self, monkeypatch):
        _, gateway = _install_gateway(monkeypatch, "", "A Fine Generated Fallback Title")

        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_modification_input": True,
                "original_user_content": "Original content.",
                "suggested_content": "Current suggestion text.",
                "quality_iteration_count": 1,
            },
            user_input="Make it more formal",
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Current suggestion text."
        assert gateway.generate.await_count == 2


# ---------------------------------------------------------------------------
# Quality-decision resume branch (awaiting_quality_decision): options 1-4
# ---------------------------------------------------------------------------


class TestResumeQualityDecision:
    @pytest.mark.asyncio
    async def test_option_1_accepts_suggested_version(self, monkeypatch):
        _install_gateway(monkeypatch, "A Fine Generated Title Text")
        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_quality_decision": True,
                "suggested_content": "Suggested content.",
                "document_content": "Original content.",
            },
            user_input="1",
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Suggested content."

    @pytest.mark.asyncio
    async def test_cancel_word_cancels_ingestion(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(
            packet_state={"input_mode": "interactive", "awaiting_quality_decision": True},
            user_input="cancel",
        )

        result = await ic.improve_content(ctx)

        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_option_4_cancels_ingestion(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(
            packet_state={"input_mode": "interactive", "awaiting_quality_decision": True},
            user_input="4",
        )

        result = await ic.improve_content(ctx)

        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_option_2_below_cap_asks_for_modification_instructions(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_quality_decision": True,
                "quality_iteration_count": 0,
            },
            user_input="2",
        )

        result = await ic.improve_content(ctx)

        assert result.needs_user_input is True
        assert result.state_updates == {
            "awaiting_quality_decision": False,
            "awaiting_modification_input": True,
        }

    @pytest.mark.asyncio
    async def test_option_2_at_cap_accepts_current_suggestion_instead(self, monkeypatch):
        _install_gateway(monkeypatch, "A Fine Generated Title Text")
        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_quality_decision": True,
                "quality_iteration_count": ic.MAX_QUALITY_ITERATIONS,
                "suggested_content": "Suggested content.",
            },
            user_input="2",
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Suggested content."

    @pytest.mark.asyncio
    async def test_option_3_uses_original_content(self, monkeypatch):
        _install_gateway(monkeypatch, "A Fine Generated Title Text")
        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_quality_decision": True,
                "original_user_content": "Original text.",
                "suggested_content": "Suggested text.",
            },
            user_input="3",
        )

        result = await ic.improve_content(ctx)

        assert result.data["improved_content"] == "Original text."

    @pytest.mark.asyncio
    async def test_invalid_choice_reprompts(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(
            packet_state={"input_mode": "interactive", "awaiting_quality_decision": True},
            user_input="banana",
        )

        result = await ic.improve_content(ctx)

        assert result.needs_user_input is True
        assert "1, 2, 3, or 4" in result.user_prompt


# ---------------------------------------------------------------------------
# Passthrough for non-manual input (Google Drive documents)
# ---------------------------------------------------------------------------


class TestPassthroughForNonManualInput:
    @pytest.mark.asyncio
    async def test_google_drive_input_mode_skips_and_never_touches_the_llm(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(packet_state={"input_mode": "google_drive"})

        result = await ic.improve_content(ctx)

        assert result.data == {}
        assert "Skipping" in result.progress_message

    @pytest.mark.asyncio
    async def test_missing_input_mode_also_passes_through(self, monkeypatch):
        _forbid_gateway(monkeypatch)
        ctx = _make_context(packet_state={})

        result = await ic.improve_content(ctx)

        assert result.data == {}


# ---------------------------------------------------------------------------
# Per-call-site model-tier resolution (docs/superpowers/plans/
# 2026-08-08-model-tier-selection.md). Each of the three _call_gemini call
# sites must resolve ITS OWN prompt's tier -- naming, modification and
# quality_eval are independent prompts that happen to share the _call_gemini
# helper, so a regression here looks like all three silently collapsing back
# onto one hardcoded/shared model.
# ---------------------------------------------------------------------------

# None of the three bundled ingestion.improve_content.* prompts currently
# override `model:`, so they all default to the same "fast" tier today
# (shared/prompts/spec.py). Left alone, that coincidence would hide a call
# site that reads the WRONG prompt id's tier (e.g. naming accidentally using
# modification's spec) -- the resolved model would still happen to match.
# Force each id to a distinct tier for this class only, so a mismatch is
# only silent when the code is actually querying the right id.
_CALL_SITE_TIERS = {
    "ingestion.improve_content.naming": "thinking",
    "ingestion.improve_content.modification": "fast",
    "ingestion.improve_content.quality_eval": "lite",
}


def _install_distinct_tiers(monkeypatch):
    real_spec = ic.PROMPTS.spec

    def _fake_spec(prompt_id: str):
        spec = real_spec(prompt_id)
        tier = _CALL_SITE_TIERS.get(prompt_id)
        return dataclasses.replace(spec, model=tier) if tier else spec

    monkeypatch.setattr(ic.PROMPTS, "spec", _fake_spec)


class TestCallSiteModelResolution:
    @pytest.mark.asyncio
    async def test_naming_call_site_resolves_its_own_prompt_tier(self, monkeypatch):
        _install_distinct_tiers(monkeypatch)
        expected_model = ic.resolve_model(
            ic.PROMPTS.spec("ingestion.improve_content.naming").model
        )
        factory, gateway = _install_gateway(monkeypatch, "A Perfectly Good Five Word Title")

        await ic._auto_generate_title("some content", "technical", "Jane Doe")

        factory.assert_called_once_with(default_model=expected_model)
        options = gateway.generate.call_args.args[1]
        assert options.model == expected_model
        assert options.response_format is None  # naming calls with json_output=False

    @pytest.mark.asyncio
    async def test_modification_call_site_resolves_its_own_prompt_tier(self, monkeypatch):
        _install_distinct_tiers(monkeypatch)
        expected_model = ic.resolve_model(
            ic.PROMPTS.spec("ingestion.improve_content.modification").model
        )
        factory, gateway = _install_gateway(monkeypatch, "A revised version of the content.")

        ctx = _make_context(
            packet_state={
                "input_mode": "interactive",
                "awaiting_modification_input": True,
                "original_user_content": "Original content.",
                "suggested_content": "Suggested content.",
            },
            user_input="Make it punchier",
        )
        result = await ic.improve_content(ctx)

        # needs_user_input (rather than a finalized result) confirms only the
        # modification call site fired -- a naming-call cascade would mean
        # this test isn't isolating what it claims to.
        assert result.needs_user_input is True
        factory.assert_called_once_with(default_model=expected_model)
        options = gateway.generate.call_args.args[1]
        assert options.model == expected_model
        assert options.response_format is None  # modification calls with json_output=False

    @pytest.mark.asyncio
    async def test_quality_eval_call_site_resolves_its_own_prompt_tier(self, monkeypatch):
        _install_distinct_tiers(monkeypatch)
        expected_model = ic.resolve_model(
            ic.PROMPTS.spec("ingestion.improve_content.quality_eval").model
        )
        payload = json.dumps(
            {"is_good": False, "reasoning": "needs work", "suggested_version": "Better text."}
        )
        factory, gateway = _install_gateway(monkeypatch, payload)

        ctx = _make_context(
            packet_state={"input_mode": "interactive", "document_content": "Raw content."}
        )
        result = await ic.improve_content(ctx)

        # needs_user_input confirms no finalize cascade, so this run is only
        # the quality_eval call site.
        assert result.needs_user_input is True
        factory.assert_called_once_with(default_model=expected_model)
        options = gateway.generate.call_args.args[1]
        assert options.model == expected_model
        assert options.response_format == "json"  # quality_eval calls with json_output=True
