# Graceful Execution-Limit Recovery Design

## Goal

Replace generic failures caused by tool-loop, graph-step, or response-size limits with a persisted, user-facing recovery summary that explains completed work, remaining work, and how to continue.

## Scope

The recovery applies to ordinary chat requests and scheduled user commands. It covers:

- exhaustion of the configured tool-round budget;
- model output truncated by a provider output limit;
- an unexpected LangGraph recursion-limit exception as a defensive fallback.

It does not retry or undo completed tool actions. In particular, Jira mutations already issued before a limit is reached remain recorded as completed work.

## Current Failure

`MAX_TOOL_ROUNDS` is intended to stop an overlong tool loop. The full LangGraph invocation, however, uses a fixed recursion limit of 50. With a production tool-round budget of 20, the graph can consume the recursion budget before it reaches the normal round-limit branch. The exception bypasses `respond` and `save_history`, leaving the scheduler to deliver a generic error without a continuation record.

## Chosen Architecture

Use the graph's existing round-limit branch as the normal exit point and generalize its existing no-tools partial synthesis.

1. Derive the LangGraph recursion budget from the configured tool-round budget plus fixed graph overhead. This gives the graph enough steps to enter the normal limit handler after the final allowed tool round.
2. Add a small, shared recovery formatter that supplies a fixed user-facing preamble and continuation instruction.
3. Run a final, tools-disabled synthesis only when accumulated work exists. Its prompt must summarize only information already present in the current turn's history and tool results; it must not call MCP tools or perform additional mutations.
4. Route the resulting recovery text through the ordinary `respond` and `save_history` nodes. The stored assistant message becomes the authoritative continuation context for the user's next ordinary chat message. Scheduled runs also save it, but remain stateless during the scheduled execution itself.
5. Treat `MAX_TOKENS` and proactive no-progress/tool-budget termination as the same recovery category, with a reason-specific preamble.
6. Catch `GraphRecursionError` in the webhook boundary only as a defensive fallback. It returns an honest retry instruction and logs structured context, but is not relied on for partial-work reconstruction.

## User Message Contract

Every graceful-limit response must contain:

- a clear statement that the single task reached its processing limit;
- a reason suitable for the condition (`task took too long`, `tool loop stopped`, or `response was too large`);
- a bounded summary under **Completed**;
- a bounded summary under **Remaining**;
- an instruction to reply with `continue` or repeat the request to continue from the recorded summary.

The message must never claim an action succeeded unless its tool result reports success. If no reliable completed-work details are available, it says so plainly.

## Relationship to Verification

This is not part of `ResponseVerificationService`. Verification evaluates a completed response; execution-limit recovery must decide how to finish an incomplete graph without calling further tools. The recovery synthesis may be verified only if the existing graph routing already verifies terminal text, but verification must not trigger another tool loop or hide the continuation summary.

## Safety and Observability

- The recovery synthesis receives `tools_payload=None`.
- It adds no retries and makes no write calls.
- Logs record the limit reason, completed tool-call count, successful-result count, failed-result count, and whether a synthesis was produced.
- The standard response persistence path records the user-visible summary for subsequent ordinary chat turns.

## Tests

Tests will prove that:

1. a configured tool-round limit reaches recovery before LangGraph's recursion guard;
2. ordinary and scheduled requests return the same recovery contract and preserve the normal persistence path;
3. `MAX_TOKENS` produces recovery rather than a silently truncated response;
4. the tools-disabled synthesis cannot receive a tool payload;
5. the defensive recursion fallback remains safe and descriptive.
