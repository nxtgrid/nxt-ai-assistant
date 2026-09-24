# OpenRouter Jev Decisions Design

## Intent and scope

Add an optional, independently configured OpenRouter Jev decision path for five existing bounded judgments: outgoing response verification, first-message issue type, ticket-comment significance, multi-active-thread assignment, and conversation-context filtering. The operator controls one master switch and one Jev model picker in Anansi App's **AI Models & Providers** settings. The switch defaults off. The existing generation provider and model tiers remain independent; a deployment using Gemini for chat may use OpenRouter only for Jev decisions.

Success means each selected call site can use the same typed Decisions API client, preserve its existing result contract and fallback behavior, and report which path made the decision. This design does not route ordinary generation, expert argument extraction, document ingestion classification, alert correlation, or technical-response rewriting through Jev.

## Documented wire contract

As checked on 2026-09-24, OpenRouter serves Jev through `POST https://openrouter.ai/api/alpha/decisions`, **not** `/api/v1/chat/completions`. The requested latest alias is `~typesafe/jev-latest`; the currently documented pinned choice is `typesafe/jev-1.13`. A request has `model`, text or JSON `state`, and a map of `questions`. A `choice` question has `type`, `instructions`, and labeled `criteria`; a `noul` has `type`, `instructions`, and optional `criteria: {"true": ..., "false": ...}`. A Choice answer contains `type`, `choice`, `probabilities`, and `confidence`; a Noul answer contains `type` and `noul`. Raw HTTP usage keys are snake_case, and the response may include a resolved model, provider, usage, and id. Send an OpenRouter bearer key and JSON content type. Do not send chat messages, temperature, response format, OpenRouter generation-provider preferences, or SDK-only `decisionsRequest` wrapping in raw HTTP.

Primary references: [OpenRouter Jev raw HTTP examples](https://openrouter.ai/blog/insights/what-is-jev/), [Jev Latest model page](https://openrouter.ai/~typesafe/jev-latest/), [OpenRouter judge comparison](https://openrouter.ai/blog/tutorials/jev-vs-llm-as-a-judge/), [TypeSafe question semantics](https://docs.typesafe.ai/primitives/choice), [TypeSafe model limits and language support](https://docs.typesafe.ai/models), and [TypeSafe known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13). OpenRouter's Decisions API is marked alpha; the implementation must pin and test this documented request/response shape, and recheck it before a live rollout.

## Architecture and settings

Create a small async `OpenRouterDecisionClient` in `shared/llm/openrouter_decisions.py`, separate from `OpenRouterGateway` because that gateway is for chat completions. It accepts an injected `httpx.AsyncClient` for offline contract tests, uses `OPENROUTER_API_KEY` (with the existing `OPEN_ROUTER_BEARER_TOKEN` compatibility alias), and posts to a dedicated Decisions URL. Bound each call with a short explicit timeout. Validate required question ids and answer types, Choice membership and finite 0–1 probabilities/confidence, and finite 0–1 Noul values. Unknown or incomplete responses are errors. Log request id, resolved model, latency, usage/cost when present, outcome source, and error class; never log customer text, credentials, or raw model state. No automatic retries in the synchronous customer path; a later retry policy needs measured benefit.

Add registry settings `JEV_DECISIONS_ENABLED=false` and `JEV_DECISIONS_MODEL=~typesafe/jev-latest` to group `models`. The single switch controls all five adapters; existing `VERIFICATION_ENABLED`, `THREAD_DISENTANGLEMENT_ENABLED`, and `CONTEXT_FILTER_ENABLED` still gate their respective features. The Jev switch does not depend on `LLM_PROVIDER=openrouter`. The model picker is a dedicated searchable select, populated from OpenRouter model discovery filtered to Jev IDs, with the documented latest alias and pinned current model as fallbacks. Preserve a configured current value if discovery fails, following the existing picker pattern. Reject blank/non-Jev values on save and at runtime. Show a readiness note when the switch is on and no OpenRouter key is configured; do not attempt a Jev call without the key. Save through the existing settings backend, with no new secret field or database migration.

Do not assume OpenRouter's chat provider-order options apply to Decisions: the documented Decisions request contains no provider routing object. Keep this choice explicit in UI help and code. The selected alias can move to a new model version; log the resolved model so metrics can be split by version. A pinned ID can be selected for calibration.

## Use-case adapters and policy

All Jev adapters live near their current consumers and return the consumer's existing result type. A shared `is_jev_enabled()` checks the master flag and usable key at call time. When disabled, each consumer takes its current path without constructing a Jev client. On a transport error, 402/429/5xx, timeout, invalid payload, or unsupported model, fall back to the **existing LLM call** for that use case. If that call also fails, retain its current final fallback. Record `jev`, `legacy_llm`, or `fallback` without changing user-visible output. Do not let Jev's different confidence semantics silently reuse an LLM-produced threshold.

### Response verification

The current `ResponseVerificationService.verify_response()` returns `passed`, free-form `feedback`, and failed `categories`; failed replies regenerate with feedback and may escalate after repeated failures. Keep that interface. Send the user question, draft answer, mode, relevant recent context, criteria, and compact tool evidence as `state`. Ask independent Nouls for observable violations: answer misses the question (reply mode only), factual claim unsupported by **provided** evidence, internal identifiers/details exposed, unsupported certainty/guessing, and inappropriate tone or unclear wording. Broadcast mode uses its own applicable questions and never fails for not answering an individual question. Map question ids to existing category names in code.

Jev is a **fast-pass gate** initially. Only a message with every applicable violation probability below a conservative, separately calibrated per-criterion pass threshold skips the legacy judge. The fast pass checks safety, all configured criteria, and available-tool awareness in addition to the specific questions above; it is ineligible if essential inputs exceed its bounds. Any positive/uncertain question, missing evidence for a claimed factual check, or malformed Jev answer goes to the existing LLM judge, which supplies the written feedback required for regeneration. No template feedback is invented for factual errors. This preserves the repair and escalation flow and limits what an unvalidated Jev result can change. The existing technical sanitizer remains generative. The verifier's current tool context mainly supplies tool names and success/failure, not full result facts; before treating Jev as a factual verifier, pass bounded, redacted source excerpts or tool results needed for that claim, or route the item to the legacy judge. A claim cannot be marked source-supported merely because a tool succeeded.

### 1. Issue type

Replace the model decision in `classify_issue_type()` with a Choice over `token`, `hps`, `meter`, `transaction`, `commissioning`, and `other`, using the current category definitions. Validate the chosen label and require a locally calibrated confidence floor; an uncertain answer invokes the legacy LLM. Preserve `other` if both fail. Explicit new-issue and thread rules remain deterministic.

### 2. Ticket comment significance

Keep the deterministic short-comment noise filter. Ask one Noul whether the comment warrants interrupting the Telegram group, with positive and negative criteria mirroring `_SIGNIFICANCE_SYSTEM`. A calibrated high/low band returns True/False; the middle band invokes the legacy LLM. If both fail, retain the current False (fail-closed) policy. Track false positives and missed significant updates separately.

### 3. Thread assignment

Only the existing multiple-active-thread branch changes. Use a Choice over the active thread ids and `NEW`, with each thread's last three compact messages as criteria/state. Validate that the selected id belongs to the candidate set. Low confidence invokes the legacy LLM. Keep reply chains, slash commands, explicit new-issue signals, active work packets, and zero/one-active-thread paths deterministic. If both models fail, retain the current new-thread fallback.

### 6. Context filtering

Only run when `CONTEXT_FILTER_ENABLED` is already on and thread filtering has not taken precedence. Ask one Noul per candidate message in a single Decisions request: whether that candidate is needed to understand or answer the incoming message. Preselect bounded candidates exactly as today, then preserve the existing tool-call/tool-result pair enforcement. A clearly relevant item is retained; an uncertain item is also retained to avoid dropping useful context. If the aggregate result is unusable or all candidates would be removed, invoke the legacy filter, then retain all if that fails. Measure downstream answer quality and input-token savings, not merely the number of messages removed.

## Thresholds and rollout

Do not treat TypeSafe `confidence` as the probability that a choice is correct, and do not transfer existing LLM thresholds to Jev. Keep initial thresholds in a small named policy module with rationale and model version. Before any production enablement, collect privacy-safe labeled examples for each path, compare Jev with the current call, and set thresholds based on false-pass/false-drop costs. Log decision distributions and model version; add a shadow evaluation path only if it can be run with consent and funded credit. The global switch remains off until a funded live smoke test and calibration pass. English is the model's strongest language; include supported customer languages in evaluation.

## Offline verification and live limitation

No OpenRouter credit is available now, so development cannot confirm a successful Jev response from the live endpoint. Offline tests must inspect the exact URL, headers, raw JSON body, typed response parsing, 402/429/timeouts, bad/partial JSON, unknown labels, and fallback order using an injected HTTP transport. Add adapter tests for pass/fail/uncertain cases, mode differences, deterministic bypasses, missing key, setting persistence, model picker options, and flag-off parity. A funded smoke-test checklist is a release gate, not a test claimed to have run: one Choice, one Noul, one batched multi-question request via `~typesafe/jev-latest`, verify raw response shape/model/usage, then one end-to-end case per adapter. Do not enable the flag in production until this gate and labeled evaluation pass.

## Out of scope

Jev cannot generate repair feedback or rewritten prose. The generation gateway and `LLM_PROVIDER` setting do not change. This spec makes no accuracy or cost-saving claim before measurement and does not add new customer data to tracked fixtures, docs, commits, or PR text.
