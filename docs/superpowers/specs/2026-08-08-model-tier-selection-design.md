# Model tier selection: thinking / fast / lite

**Date:** 2026-08-08
**Status:** Draft, pending approval

## Problem

Model selection is currently ~7 scattered, inconsistently-named environment variables (`GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `GEMINI_DEEP_THINKING_MODEL`, `GEMINI_AGENT_PRO_MODEL`, `VERIFICATION_MODEL`, `THREAD_CLASSIFIER_MODEL`, `INTENT_ROUTER_MODEL`), read independently by ~10+ call sites across `chat_orchestrator`. Some of this is accidental: `VERIFICATION_MODEL` is read by three unrelated services (`verification_service.py`, `conversation_summarizer.py`, `context_filter.py`) that have nothing to do with each other, just because they happened to want the same cheap model at some point. Two ingestion prompts have no dedicated variable at all and silently ride the bare `GEMINI_MODEL` default. `command_registry.py` has its own separate `model_override` mechanism (a field holding an *env var name*, not a model string) for slash-commands. `experts.definitions.prompt` — the prompt that defines every expert workflow — embeds at least one of these env var names (`GEMINI_AGENT_PRO_MODEL`) directly in its content, not just in code.

Goal: collapse this to exactly three named tiers -- `thinking`, `fast`, `lite` -- each prompt declares which one it uses, and an admin can change a prompt's tier live from the Prompts page without a PR.

## Decisions

### 1. Tier-to-model mapping: three new environment variables, replacing all existing ones

`MODEL_THINKING`, `MODEL_FAST`, `MODEL_LITE` — literal environment variables, set at deploy time exactly like `GEMINI_MODEL` is today. **Not** admin-UI-editable; changing what a tier *means* still goes through a deploy, same as today's model env vars always have. A new `resolve_model(tier: Literal["thinking", "fast", "lite"]) -> str` helper (`shared/llm/`, next to `factory.py`) reads these three and replaces every other model env var read in the codebase.

`GEMINI_FALLBACK_MODEL`'s role (used when the primary call fails, not a quality tier) is a different concept from the other six and doesn't have an obvious tier home -- see Open Questions.

### 2. Per-prompt tier choice: live, admin-editable, no PR

This is the piece that actually needs new infrastructure, and it mirrors the DB-override design already discussed (and set aside) for permissions -- now with a concrete use:

- `PromptSpec.model` (already in the frontmatter schema, currently `Optional[str]`, zero consumers) becomes a required `Literal["thinking", "fast", "lite"]`. Every `.prompt` file declares its starting tier; parsing rejects any other value.
- New `prompt_model_overrides` table: `prompt_id` (PK), `tier`, `updated_at`, `updated_by` -- same shape as `prompt_doc_bindings`. A row present means the override is live; absent means frontmatter's `model` is authoritative. Same TTL-cache-with-graceful-fallback pattern `OverrideStore` already uses for labels and doc bindings.
- `PromptLibrary.spec()` merges it in -- the same single choke point identified during the permissions design, so every consumer (the admin UI and the actual render/call-site resolution) sees one consistent answer.
- New "Tier" dropdown in the Prompts admin page's detail dialog, saving directly to `prompt_model_overrides`.

### 3. Who can change a prompt's tier: reuse existing edit/publish grants

No new permission axis. If a prompt's `access.edit`/`access.publish` already lets ops/eng touch its body, the same groups can change its tier. Keeps this orthogonal to the permissions work rather than inventing a second grants system.

### 4. `command_registry.py`'s `model_override` also migrates

Its one confirmed usage (`model_override="GEMINI_DEEP_THINKING_MODEL"`, line 807) changes to reference a tier name directly instead of an env var name, resolved through the same `resolve_model()` helper -- no second, parallel model-selection mechanism left in the codebase.

## What's confidently known vs. what needs a full audit

Given how deep this reaches (a prompt's *content*, not just code, references one of these env vars; `shared/llm/pricing.py` and `shared/config/flag_registry.py` both key off the exact variable names for cost tracking and flag registration respectively), I'm not going to claim a complete migration table here that I haven't fully verified. What's confirmed so far:

| Signal | Likely tier | Confidence |
|---|---|---|
| `thread_assignment.classify` (default `gemini-2.5-flash-lite` via `THREAD_CLASSIFIER_MODEL`) | lite | High |
| `conversation.summarize`, `context_filter.relevance` (both via shared `VERIFICATION_MODEL`, default `gemini-2.5-flash-lite`) | lite | High |
| `verification.sanitize` (via `VERIFICATION_MODEL`) | lite | Medium -- `verification.criteria` and `verification.sanitize_system`'s exact consumers span `verification_service.py`, `instructions_provider.py`, `app.py`, and `settings.py`; not fully traced |
| `ingestion.classify_document`, `ingestion.detect_contradictions`, `ingestion.extract_entities`, `ingestion.improve_content.*` (bare `GEMINI_MODEL` default via a shared `_call_gemini` helper) | fast | Medium |
| `intent_router.route` (`INTENT_ROUTER_MODEL` or `GEMINI_FALLBACK_MODEL`) | fast | Low -- don't know the current deployed value of `INTENT_ROUTER_MODEL` |
| The `command_registry.py` command using `GEMINI_DEEP_THINKING_MODEL`, and whatever in `experts.definitions.prompt` uses `GEMINI_AGENT_PRO_MODEL` | thinking | High on tier, but the expert-definitions content edit itself needs its own careful pass -- it's prompt *text* an LLM reads, not code |
| The other ~14 prompts (no dedicated env var today, never had explicit selection) | Needs assignment | None yet -- literally never been considered as a tier question before |

**First task of the implementation plan, before any code changes:** a complete grep/read audit of every reference to all seven existing env var names (`GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `GEMINI_DEEP_THINKING_MODEL`, `GEMINI_AGENT_PRO_MODEL`, `VERIFICATION_MODEL`, `THREAD_CLASSIFIER_MODEL`, `INTENT_ROUTER_MODEL`) across the whole repo -- code, prompt content, tests, `.env.example`, `flag_registry.py`, `pricing.py` -- producing the exhaustive table this design doc doesn't have yet. Assigning a wrong tier silently changes which model a prompt uses; guessing isn't good enough here the way it was for permissions' boolean flags.

## Non-goals

- `EMBEDDING_MODEL` (used by `get_default_embedding_gateway()`) is untouched -- embeddings are a different capability than prompt completion, not part of "the models are hardcoded env vars relating to prompts."
- `GEMINI_FALLBACK_MODEL`'s failure-fallback role isn't necessarily folded into a tier -- flagged as an open question, not decided here.
- No changes to `access.py`/permissions -- this reuses existing grants, doesn't extend them.

## Open questions for plan time (not blocking design approval, but need answers before writing bite-sized tasks)

1. What does `GEMINI_FALLBACK_MODEL` become? A 4th env var outside the 3 tiers (kept as-is, since "all model variables need to go away" may not have meant *this* one, which is a different concept), or does one of the 3 tiers absorb its role?
2. Exact starting tier for the ~14 prompts that have never had explicit model selection.
3. Whether `shared/llm/pricing.py`'s cost-tracking logic needs updating to key off tier names instead of model env var names, or whether it already keys off the resolved model string (in which case it's unaffected).

## Testing / verification

1. `resolve_model()` unit tests: all three tiers resolve to the right env var, unset env var behavior is explicit (error, not silent empty string).
2. `test_prompt_spec.py`-style parser tests: `model` frontmatter field rejects anything outside the 3 literals.
3. Every one of the ~10+ call sites' existing tests still pass after switching from direct `os.getenv()` reads to `resolve_model(PROMPTS.spec(id).model)`.
4. `test_flag_registry.py` updated for the retired flags (`GEMINI_AGENT_PRO_MODEL` etc.), mirroring how `test_prompt_misc.py` already tracks "prompts that left flag_registry."
5. Admin UI: manually verify the Tier dropdown saves to `prompt_model_overrides` and that reverting removes the override row, resolving back to frontmatter's `model`.
