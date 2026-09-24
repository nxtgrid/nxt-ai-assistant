# OpenRouter Jev Decisions Implementation Plan

## Implementation status (2026-09-24)

The client, settings, five adapters, offline tests, and operator documentation have been implemented on the feature branch. The task checkboxes below preserve the original build sequence rather than claiming every suggested test was run verbatim. The focused integration suite passed 138 tests, with two graph tests deselected because this checkout lacks `psycopg`. A separate verification lifecycle test also stops on that missing dependency. No funded live Jev call has run; the switch remains off by default. The funded smoke test and labeled threshold calibration below remain release gates before production enablement.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an off-by-default OpenRouter Jev Decisions path for response verification, issue type, ticket-comment significance, multi-thread assignment, and context filtering, with an Anansi App settings switch and model dropdown.

**Architecture:** A dedicated async HTTP client implements OpenRouter's alpha Decisions wire contract. Small adapters at five existing consumers translate typed answers into their current return types and use the current LLM path when Jev is unavailable or uncertain. A model registry setting controls all adapters independently of the generation provider.

**Tech Stack:** Python, `httpx`, existing shared flag registry and NiceGUI settings, pytest with mocked HTTP transport.

**Spec:** `docs/superpowers/specs/2026-09-24-openrouter-jev-decisions-design.md`

## Global Constraints

- Request URL: `https://openrouter.ai/api/alpha/decisions`; raw JSON keys: `model`, `state`, `questions`.
- Default model: `~typesafe/jev-latest`; documented pinned option: `typesafe/jev-1.13`.
- `JEV_DECISIONS_ENABLED` defaults false and is independent of `LLM_PROVIDER`.
- Keep `VERIFICATION_ENABLED`, `THREAD_DISENTANGLEMENT_ENABLED`, and `CONTEXT_FILTER_ENABLED` as existing gates.
- Never send Decisions requests to `/api/v1/chat/completions` or put `messages`, `temperature`, `response_format`, `provider`, or SDK-only `decisionsRequest` in their raw body.
- No live Jev success test is possible without OpenRouter credit. Keep production disabled until funded smoke tests and labeled threshold evaluation pass.
- Use synthetic names, ids, and messages in tests, documentation, commit messages, and PR text. Do not use Spiral.

## File Structure

- `shared/llm/openrouter_decisions.py`: async Decisions transport, wire validation, typed response access, and safe telemetry.
- `shared/llm/jev_policy.py`: enablement/model settings and named, versioned decision thresholds.
- `shared/config/flag_registry.py`: registry flags and model-setting metadata.
- `anansi_app/services/settings_service.py`: Jev-only model discovery with documented fallback options.
- `anansi_app/nicegui_app/pages/settings.py`: model-section switch, independent Jev dropdown, help/readiness text.
- Existing consumer modules: `verification_service.py`, `thread_assignment.py`, `ticketing/update_render.py`, and `context_filter.py`; keep each adapter close to its current fallback.
- Existing test modules in `shared/tests/`, `anansi_app/tests/`, and `chat_orchestrator/tests/services/` receive synthetic offline cases.

## Review Focus

1. An operator chooses Gemini generation and enables Jev: the Decisions call must still use the OpenRouter key and endpoint. Task 2 tests this.
2. The OpenRouter key exists but the account has no credit: HTTP 402 must trigger the legacy call without dropping a customer reply or ticket update. Tasks 1 and 3 test this.
3. A high-confidence verifier answer has no evidence for a factual claim: it must reach the legacy judge rather than fast-pass. Task 3 tests this.
4. A thread choice names an id outside the active set: it must never assign that id. Task 5 tests this.
5. Context filtering classifies every item irrelevant: it must not send an empty history to the answering model. Task 6 tests this.

---

### Task 1: Decisions HTTP client and policy boundary

**Files:** Create `shared/llm/openrouter_decisions.py`, `shared/llm/jev_policy.py`, `shared/tests/test_openrouter_decisions.py`.

**Interfaces:** `OpenRouterDecisionClient.decide(*, state: str | dict, questions: dict[str, dict], model: str) -> DecisionResponse`; `DecisionResponse.choice(question_id, allowed_keys) -> ChoiceAnswer`; `DecisionResponse.noul(question_id) -> float`; `is_jev_enabled() -> bool`; `jev_model() -> str`. Define a distinct `DecisionContractError`. The response wrapper exposes `resolved_model`, `request_id`, `usage`, and `cost` without assuming they are present.

- [ ] **Step 1: Write failing contract tests.** Use `httpx.MockTransport` to capture one request and return a documented-shaped synthetic response:

  ```python
  assert request.url == "https://openrouter.ai/api/alpha/decisions"
  assert payload == {
      "model": "~typesafe/jev-latest",
      "state": {"message": "Example issue"},
      "questions": {"issue": {"type": "choice", "instructions": "Which category?", "criteria": {"meter": "Meter faults", "other": "Everything else"}}},
  }
  assert result.choice("issue", {"meter", "other"}).choice == "meter"
  ```

  Add a Noul test and parametrized bad responses: missing answer, wrong `type`, unknown choice, NaN/out-of-range probability, non-JSON body, 402, 429, and timeout. Assert errors contain no bearer token or state text. Also assert alias credentials use `OPEN_ROUTER_BEARER_TOKEN` only when the primary key is absent.

- [ ] **Step 2: Run the new test file and confirm it fails.** Run `PYTHONPATH=. pytest shared/tests/test_openrouter_decisions.py -q`.
- [ ] **Step 3: Implement the smallest async client.** Post using injected or owned `httpx.AsyncClient` with bearer and JSON headers; build the exact raw body; validate answer shape and numeric finiteness; expose optional raw usage fields; bound timeout; close only an owned client. Avoid routing through `OpenRouterGateway` and avoid automatic retries. Keep `jev_policy.py` limited to flag/key/model resolution and named threshold constants marked as provisional until calibration.
- [ ] **Step 4: Run `PYTHONPATH=. pytest shared/tests/test_openrouter_decisions.py -q`; expect pass.** Review the captured body against [OpenRouter's raw HTTP example](https://openrouter.ai/blog/insights/what-is-jev/) once more.
- [ ] **Step 5: Commit this unit** with a generic message such as `feat: add OpenRouter decision client`.

### Task 2: Settings registry and model picker

**Files:** Modify `shared/config/flag_registry.py`, `anansi_app/services/settings_service.py`, `anansi_app/nicegui_app/pages/settings.py`; extend `anansi_app/tests/test_model_settings.py`, `anansi_app/tests/test_settings_page.py`, and relevant registry tests.

**Interfaces:** Registry keys `JEV_DECISIONS_ENABLED: bool`, `JEV_DECISIONS_MODEL: str`; `SettingsService.get_jev_models() -> list[str]` returns Jev-family model ids with `~typesafe/jev-latest` and `typesafe/jev-1.13` as fallbacks. The UI supplies `model_options["JEV_DECISIONS_MODEL"]` to its existing select renderer.

- [ ] **Step 1: Add failing tests** for default-off values, group `models`, persistence, provider independence, model picker options, invalid blank/non-Jev values, preserved current value on failed discovery, and a readiness note with missing key. Example invariant:

  ```python
  pending = {"LLM_PROVIDER": "gemini", "JEV_DECISIONS_ENABLED": True,
             "JEV_DECISIONS_MODEL": "~typesafe/jev-latest"}
  assert "JEV_DECISIONS_MODEL" in _model_select_options(fake_service, pending)
  assert _model_section_plan(["JEV_DECISIONS_ENABLED", "JEV_DECISIONS_MODEL"], pending)
  ```

- [ ] **Step 2: Run focused tests and confirm failure.** Run `PYTHONPATH=.:anansi_app pytest anansi_app/tests/test_model_settings.py anansi_app/tests/test_settings_page.py -q`.
- [ ] **Step 3: Add registry flags and UI wiring.** Put the switch and searchable dropdown in `AI Models & Providers` regardless of `LLM_PROVIDER`. Filter discovered OpenRouter ids to the Jev family and deduplicate with the two documented fallbacks. Add server-side validation before saving and runtime validation in `jev_model()`; preserve an existing Jev-family value when discovery fails. Explain that OpenRouter key/credit are required and that chat provider routing controls do not apply to Decisions.
- [ ] **Step 4: Run the focused tests, then registry tests; expect pass.** Include a test showing that toggling `LLM_PROVIDER` never rewrites the Jev model.
- [ ] **Step 5: Commit this unit** with a generic message such as `feat: expose Jev decision settings`.

### Task 3: Response-verification fast-pass

**Files:** Modify `chat_orchestrator/orchestrator/services/verification_service.py` and, only if needed for bounded evidence, `chat_orchestrator/orchestrator/graphs/conversation_graph.py`; extend `chat_orchestrator/tests/services/test_verification_service_lifecycle.py`, `test_reply_verification_escalation_guidance.py`, and `test_broadcast_verification_prompt.py`.

**Interfaces:** Preserve `ResponseVerificationService.verify_response(...) -> VerificationResult`. Add a private `_try_jev_fast_pass(...) -> bool | None`: `True` means all applicable criteria are safely clear; `None` means call the existing generative judge. Never manufacture free-form feedback from Jev.

- [ ] **Step 1: Add failing tests** that off/absent-key calls only the existing judge; all-clear Nouls skip it; any flagged or uncertain Noul calls it and retains its feedback/categories; HTTP 402 calls it; a factual claim with only `tool: succeeded` and no supporting fact calls it; broadcast mode omits reply-completeness questions. Example:

  ```python
  result = await service.verify_response("Why?", "The meter was replaced.", criteria,
                                         conversation_context="TOOLS CALLED: lookup succeeded")
  assert legacy_judge.call_count == 1  # no source evidence for replacement claim
  assert result.feedback == "Check the claimed replacement"
  ```

- [ ] **Step 2: Run `PYTHONPATH=.:chat_orchestrator pytest chat_orchestrator/tests/services/test_verification_service_lifecycle.py chat_orchestrator/tests/services/test_reply_verification_escalation_guidance.py chat_orchestrator/tests/services/test_broadcast_verification_prompt.py -q` and confirm the new tests fail.**
- [ ] **Step 3: Add bounded Jev questions and gating.** Use separate violation Nouls with literal true/false criteria, format reply versus broadcast state separately, and keep the current LLM judge for every non-clear outcome. If richer tool evidence is available, pass only the excerpts relevant to the drafted claim, with size bounds and redaction; otherwise mark factual fast-pass ineligible. Preserve Langfuse pass/fail scoring and mark `jev_fast_pass` versus `legacy_llm` in safe telemetry.
- [ ] **Step 4: Run the focused tests and existing verification graph tests; expect pass.** Explicitly check that regeneration still receives written legacy feedback on a failure and that sanitizer behavior is unchanged.
- [ ] **Step 5: Commit this unit** with a generic message such as `feat: add Jev verification fast pass`.

### Task 4: Issue type and ticket-comment significance

**Files:** Modify `chat_orchestrator/orchestrator/services/thread_assignment.py`, `chat_orchestrator/orchestrator/services/ticketing/update_render.py`; extend `chat_orchestrator/tests/services/test_thread_assignment.py` and `chat_orchestrator/tests/services/ticketing/test_update_render.py`.

**Interfaces:** Preserve `classify_issue_type(user_input) -> str` and `classify_significance(gateway, model, comment_body) -> bool`. Add private Jev adapter functions with an injected decision client in tests; keep public signatures stable.

- [ ] **Step 1: Add failing tests** for the six exact issue labels, `other`, low Choice confidence, invalid label, disabled flag, and 402 fallback. For significance, test noise bypass, clear positive, clear negative, middle-band fallback, and False when both providers fail:

  ```python
  assert await classify_issue_type("Example meter display fault") == "meter"
  assert await classify_significance(fake_gateway, "lite", "Resolved the outage and restored supply") is True
  ```

- [ ] **Step 2: Run `PYTHONPATH=.:chat_orchestrator pytest chat_orchestrator/tests/services/test_thread_assignment.py chat_orchestrator/tests/services/ticketing/test_update_render.py -q` and confirm failure.**
- [ ] **Step 3: Implement both adapters.** For issue type, use one Choice whose criteria use the current taxonomy. For significance, use one Noul with positive/negative operational examples. Keep the existing deterministic prefilter and call the existing LLM branch on uncertain/error. Do not copy the old LLM confidence threshold into Jev policy.
- [ ] **Step 4: Run focused tests; expect pass.** Inspect test logs to ensure no customer text or raw state is emitted.
- [ ] **Step 5: Commit this unit** with a generic message such as `feat: add Jev support triage decisions`.

### Task 5: Multi-active-thread assignment

**Files:** Modify `chat_orchestrator/orchestrator/services/thread_assignment.py`; extend `chat_orchestrator/tests/services/test_thread_assignment.py`.

**Interfaces:** Preserve `ThreadAssignmentService.assign_thread(...) -> ThreadAssignment | None` and `_classify_with_llm(...)` as the legacy branch. Add `_classify_with_jev(...) -> ThreadAssignment | None`, where `None` means delegate to `_classify_with_llm`.

- [ ] **Step 1: Add failing tests** for two active threads plus `NEW`, candidate-specific compact summaries, unknown returned id, low confidence, 402, and every deterministic bypass (reply, slash command, explicit new issue, active workflow, zero/one active thread). Example:

  ```python
  assert set(captured_question["criteria"]) == {"thread_a", "thread_b", "NEW"}
  assert (await service.assign_thread("Back to the first problem", history)).thread_id == "thread_a"
  ```

- [ ] **Step 2: Run `PYTHONPATH=.:chat_orchestrator pytest chat_orchestrator/tests/services/test_thread_assignment.py -q` and confirm failure.**
- [ ] **Step 3: Call Jev only from the existing Path B.** Supply stable option keys for active thread ids, describe each from last messages, validate membership, and delegate uncertain/error cases to the existing LLM path. Preserve new-thread fallback after both calls fail.
- [ ] **Step 4: Run the focused test file; expect pass.**
- [ ] **Step 5: Commit this unit** with a generic message such as `feat: add Jev multi-thread selection`.

### Task 6: Context filtering

**Files:** Modify `chat_orchestrator/orchestrator/services/context_filter.py`; extend `chat_orchestrator/tests/services/test_context_filter.py`.

**Interfaces:** Preserve `ContextFilterService.filter_history(...) -> ContextFilterResult`. Add `_try_jev_filter(...) -> ContextFilterResult | None`; `None` delegates to the existing LLM method. Ensure the current `GOOGLE_API_KEY` early check does not suppress Jev when the generation provider is Gemini or OpenRouter and the Jev key is present.

- [ ] **Step 1: Add failing tests** for one batched Noul per candidate; clearly relevant and uncertain candidates retained; tool-call/result pair closure; all-removed fallback; 402/invalid-answer legacy fallback; both-failed all-history fallback; current disabled behavior. Example:

  ```python
  assert set(captured_questions) == {"candidate_0", "candidate_1"}
  assert result.relevant_indices == [0, 1]  # an uncertain tool pair remains together
  ```

- [ ] **Step 2: Run `PYTHONPATH=.:chat_orchestrator pytest chat_orchestrator/tests/services/test_context_filter.py -q` and confirm failure.**
- [ ] **Step 3: Implement the batched filter.** Reuse current message truncation, retain uncertainty, apply `_enforce_tool_pairs`, and route invalid/empty outcomes to the existing LLM method. Ensure missing generation credentials still produce the established all-history fallback after Jev failure.
- [ ] **Step 4: Run the focused test file and a graph-level context-filter test; expect pass.**
- [ ] **Step 5: Commit this unit** with a generic message such as `feat: add Jev context relevance decisions`.

### Task 7: Offline integration review and funded release gate

**Files:** Update `README.md` settings/provider documentation and add a concise operator rollout section in this plan or a dedicated operations doc. Extend cross-module tests only for concrete gaps found in review.

**Interfaces:** No new runtime interface. Deliver a documented switch-off rollback and an unexecuted funded smoke-test checklist.

- [ ] **Step 1: Run focused suites and settings backend tests.** Suggested command: `PYTHONPATH=.:chat_orchestrator:anansi_app pytest shared/tests/test_openrouter_decisions.py shared/tests/test_settings_backends.py anansi_app/tests/test_model_settings.py anansi_app/tests/test_settings_page.py chat_orchestrator/tests/services/test_verification_service_lifecycle.py chat_orchestrator/tests/services/test_reply_verification_escalation_guidance.py chat_orchestrator/tests/services/test_broadcast_verification_prompt.py chat_orchestrator/tests/services/test_thread_assignment.py chat_orchestrator/tests/services/test_context_filter.py chat_orchestrator/tests/services/ticketing/test_update_render.py -q`.
- [ ] **Step 2: Review one captured request/response fixture against current OpenRouter documentation.** Verify alpha URL, alias spelling including leading `~`, lowercase raw `usage` keys, and absence of chat-specific fields. Check that `LLM_PROVIDER=gemini` does not affect the Decisions path.
- [ ] **Step 3: Document production gating.** With the switch off, deploy code and verify no Decisions requests occur. Once credit exists, run one Choice, one Noul, and one batched request using the selected alias, record resolved model and usage, then test one case per adapter. Evaluate labeled examples and set thresholds per path/model version before turning the switch on for customers. If the API shape has changed, update the client and offline fixtures first. Do not report these live steps as passed until actually run.
- [ ] **Step 4: Review the changed diff for customer/operator data in docs, fixtures, comments, commit messages, and PR text.** Use synthetic examples and preserve the repo's public-data rule.
- [ ] **Step 5: Commit the documentation and final test changes** with a generic message such as `docs: describe Jev rollout and verification`.

## Plan self-review

The seven tasks cover transport, settings, all five requested decisions, fallbacks, offline tests, and live rollout. The fast-pass judge retains the existing feedback-producing LLM for every non-clear result; the sanitizer remains generative. The global switch and alias are independent of the chat provider. Runtime thresholds remain provisional until labeled evaluation, and the plan does not claim live API success without credit.
