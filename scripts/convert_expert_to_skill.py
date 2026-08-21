"""Convert a prompt-only expert into a draft skill.

Phase 5, Task 14 of
docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md,
extended by Phase 7 of
docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md.

The plan named five experts as convertible on the premise that they are
"prompt-only... with no workflow at all": grid_analyst, grid_monitor,
site_visit_tracker, signing, community_sizing. Direct inspection of the live
experts.definitions doc (`python scripts/convert_expert_to_skill.py <name>`,
dry run, against each) found that premise wrong for all five:

- grid_analyst, signing, community_sizing each have real [function:...]
  workflow steps in their doc section (6, 1, and 2 respectively) -- pipeline
  experts exactly like context_expert/grids_technical_reviewer/
  ingestion_expert/package_generator, just not previously recognized as
  such. A plain text split used to silently drop that work rather than
  preserve it -- e.g. signing's actual e-signature request, or
  community_sizing's boundary detection. Phase 1-6 of the 2026-08-20 plan
  built the machinery (StepContract, tool declarations, mock mode,
  permission gating) that makes preserving it -- as a real `kind:"function"`
  step, not prose -- both possible and safe; see `_step_dict_for_body`.
- grid_monitor's doc describes "Type: persistent" with a wake schedule and
  {anchor_name}/{metadata_json}/{recent_conversations}/{weekly_summaries}
  templating. That whole mechanism (agent_worker.py, persistent_agent_graph.py,
  user_agent_service.py, expert_tool_runner.py, the messaging MCP server) was
  already fully removed -- see db/migrations/0014_drop_persistent_agents.sql,
  Phase 6 of docs/superpowers/plans/2026-08-06-user-designed-skills.md,
  which the operator confirmed against live `persistent_agent_instances`
  rows before dropping them (including grid_monitor's own paused ones). Its
  doc entry is stale relative to the current codebase, not a live expert
  waiting to be converted.
- site_visit_tracker's doc describes "Type: user_startable", also with a
  wake schedule, required inputs (site_name, visit_date), and the same
  four template variables. expert_instructions_provider.py still *parses*
  expert_type/wake_schedule/required_inputs into ExpertConfig fields, but
  nothing else in chat_orchestrator/orchestrator reads is_user_startable,
  .wake_schedule, or .required_inputs anywhere -- the scaffolding is
  vestigial, likely an artifact of the same Phase 6 removal, not a live
  execution path either.

So there is deliberately no CONVERTIBLE_EXPERTS allowlist here -- the plan's
own list was wrong in two different ways for all five names in it, and a
second hardcoded guess would carry the same risk. Eligibility is instead
the one thing this script can check mechanically, per named handler (see
`_unconvertible_function_handlers`): does every `[function:name]` marker
name a handler with a registered `StepContract`? That is necessary but not
sufficient -- it would not have caught site_visit_tracker's dead-scaffolding
problem, which has no textual signature to grep for -- so this still
requires a human to actually read the dry-run output's step text before
passing --apply, for any expert, not just trust a green exit code.

Everything that does convert lands as status='draft': reviewed and promoted
by a human, never activated by this script.

Phase 11 fix (2026-08-21): step-boundary detection used to be this script's
OWN numbered-list regex (matching a leading "1. ", "2. ", etc.), with "no
numbered lines found" falling back to treating the ENTIRE doc section -- system
instructions, tool list, cell-mapping tables, inputs/state/outputs, all of
it -- as one single opaque step. That fallback fired for every real,
currently-live expert actually dry-run against production
(grids_technical_reviewer, package_generator): neither uses numbered list
syntax at all, because `WorkflowExecutor._parse_step_line`/
`ExpertInstructionsProvider._extract_workflow` have ALWAYS treated numbering
as optional -- one workflow LINE is one step, "1. [llm] x" and "[llm] x"
parse identically. A second, drifted reimplementation of that same
line-splitting-and-packet-scoping logic here was the wrong fix (it would
have to independently keep matching `_extract_workflow`'s comment-stripping,
merged-line-splitting, and `## Packet: name` boundary detection forever).
Instead this script now calls the real `ExpertInstructionsProvider` and
converts its already-correctly-parsed `ExpertConfig.workflows[packet_type]`
list -- the exact step strings real expert execution would run -- so the
two can never drift apart again. See `build_steps_from_workflow_lines`
(the `_step_dict_for_body` per-line classification below it is unchanged;
only how the list of lines is produced has changed).

Usage:
    python scripts/convert_expert_to_skill.py grid_analyst
    python scripts/convert_expert_to_skill.py grid_analyst --apply

Needs both repo root and chat_orchestrator on PYTHONPATH (for `shared.*`
and `orchestrator.*` respectively) -- e.g. from repo root:
    PYTHONPATH=.:chat_orchestrator python scripts/convert_expert_to_skill.py grid_analyst
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any, Dict, List

_FUNCTION_MARKER_RE = re.compile(r"\[function:[a-zA-Z0-9_]+\]")
# Same marker, but anchored to the start of a step body with a capture group
# for the bare handler name -- what _step_dict_for_body/
# _unconvertible_function_handlers actually need, distinct from
# _FUNCTION_MARKER_RE's whole-match use in has_function_steps. Mirrors
# WorkflowExecutor._parse_step_line's own "[function:name] - description"
# convention exactly (see that method's docstring for the format list) so
# this script recognizes precisely what the real recipe parser does.
_FUNCTION_NAME_RE = re.compile(r"^\[function:([a-zA-Z0-9_]+)\]\s*-?\s*(.*)$", re.DOTALL)
# A leading "[llm]" marker on a non-function step body -- stripped so a
# converted step's instruction text doesn't literally start with "[llm]"
# (the real recipe parser strips this too; the converter never did). A
# small, deliberate addition beyond Task 7.2's literal [function:...] text:
# same code path, same marker-stripping intent, no correctness risk.
_LLM_MARKER_RE = re.compile(r"^\[llm\]\s*")
# Same handler-name capture as _FUNCTION_NAME_RE, but NOT anchored to the
# start of the text -- for finding every [function:name] marker anywhere in
# a whole (possibly multi-step) instruction block, e.g.
# _unconvertible_function_handlers scanning an expert's full doc section
# before it's even split into per-step bodies.
_FUNCTION_NAME_ANYWHERE_RE = re.compile(r"\[function:([a-zA-Z0-9_]+)\]")


def has_function_steps(instructions: str) -> bool:
    """Whether this expert's doc section names any [function:handler] step.

    Informational only as of Phase 7 -- see `_unconvertible_function_handlers`
    for the actual (per-handler) conversion gate `expert_to_skill` uses.
    Kept because it's still a useful, cheap "does this doc section have any
    function markers at all" predicate, and existing tests exercise it
    directly.
    """
    return bool(_FUNCTION_MARKER_RE.search(instructions or ""))


def _unconvertible_function_handlers(step_lines: List[str]) -> List[str]:
    """[function:name] markers naming a handler with no registered
    `StepContract` -- Phase 1-6's tool machinery (schema derivation,
    precondition checking, mock mode, permission gating) has nothing to
    validate, run, or mock such a handler against, so converting it would
    silently produce a step that looks real but can't actually be checked
    or safely run. Sorted, de-duplicated names for a stable, readable error
    message.

    Takes the already-isolated workflow step lines (e.g.
    `ExpertConfig.workflows[packet_type]`), not raw doc text -- a
    commented-out `// [function:...]` line never reaches this function in
    the first place (`ExpertInstructionsProvider._extract_workflow` drops
    those before `workflows` is populated), so a disabled step naming a
    contractless handler correctly does NOT block conversion of the steps
    that actually run.

    Deliberately does NOT check `required_permission` -- see
    `expert_to_skill`'s docstring for why a runtime, per-caller check isn't
    something this design-time script can usefully pre-judge.

    Local import (matches this module's existing convention of keeping
    environment-dependent imports out of module scope, e.g. `main()`'s own
    `from shared.prompts import PROMPTS`): this function, and everything
    that calls it, stays importable and testable without chat_orchestrator's
    environment as long as no test actually exercises a `[function:...]`
    marker.
    """
    from orchestrator.experts.step_registry import get_step_contract

    joined = "\n".join(step_lines or [])
    names = sorted(set(_FUNCTION_NAME_ANYWHERE_RE.findall(joined)))
    return [name for name in names if get_step_contract(name) is None]


def _step_dict_for_body(index: int, body: str, is_last: bool, preamble: str = "") -> Dict[str, Any]:
    """One skill step dict for a single already-isolated workflow line (see
    `build_steps_from_workflow_lines` -- one entry of
    `ExpertConfig.workflows[packet_type]`, numbering/bullets/comments
    already stripped by `ExpertInstructionsProvider._extract_workflow`).

    Task 7.2: a body starting with `[function:name]` becomes a
    `kind:"function"` step naming that handler, carried through intact --
    NOT flattened into prose the way every marker used to be. This is THE
    bug the whole 2026-08-20 plan exists to fix: a converted LPP/GTR/
    grid_analyst recipe's real orchestration IS these markers; discarding
    them to plain text would silently drop the actual work each step does,
    leaving a prose wrapper with none of it attached.

    A function step whose contract mutates is stamped `mutates: True` and
    defaults `mock: True` (safe by default -- an unreviewed converted
    mutating step should never fire for real until a human deliberately
    flips it) -- see skill_validation.py's `unmockable_handlers` check and
    skill_builder.py's `_render_pending_step` switch, built in Phases 5/6
    anticipating exactly this producer.

    `preamble` (only ever passed for index 0) is prepended regardless of
    kind, even to a function step's description -- it is display-only
    there (see `skill_runner.build_parsed_steps`, which reads a function
    step's real inputs from its `StepContract`, never from `instruction`),
    but dropping it when step one happens to be a function step would lose
    the expert's whole persona with nothing else to carry it.
    """
    function_match = _FUNCTION_NAME_RE.match(body)
    if function_match:
        from orchestrator.experts.step_registry import get_step_contract

        handler = function_match.group(1)
        description = function_match.group(2).strip() or handler
        if preamble:
            description = f"{preamble}\n\n{description}"

        step: Dict[str, Any] = {
            "index": index,
            "kind": "function",
            "handler": handler,
            "name": handler,
            "instruction": description,
            "is_response_step": is_last,
        }
        contract = get_step_contract(handler)
        if contract is not None and contract.mutates:
            step["mutates"] = True
            step["mock"] = True
        return step

    stripped = _LLM_MARKER_RE.sub("", body, count=1)
    if preamble:
        stripped = f"{preamble}\n\n{stripped}"
    return {
        "index": index,
        "name": f"step_{index + 1}",
        "instruction": stripped,
        "allow_write": False,
        "is_response_step": is_last,
    }


def build_steps_from_workflow_lines(
    step_lines: List[str], preamble: str = ""
) -> List[Dict[str, Any]]:
    """One skill step per already-isolated workflow line.

    `step_lines` is expected to already be what
    `ExpertInstructionsProvider._extract_workflow` produces: one string per
    step, numbering/bullets stripped, commented-out (`//`/`--`) lines
    already excluded, merged Google-Docs lines already re-split. This
    function's only remaining job is Task 7.2's per-line classification
    (`_step_dict_for_body`) -- a `kind:"function"` step for a
    `[function:name]` line, `kind:"llm"` for everything else -- plus
    prepending `preamble` to step one.

    `preamble` (the expert's system instructions) is prepended to step one
    regardless of that step's kind; dropping it would lose the expert's
    identity entirely -- see `_step_dict_for_body`'s docstring for why this
    is safe even when step one is a function step.
    """
    lines = [line.strip() for line in (step_lines or []) if (line or "").strip()]
    if not lines:
        return []

    preamble = (preamble or "").strip()
    return [
        _step_dict_for_body(
            i, line, is_last=(i == len(lines) - 1), preamble=preamble if i == 0 else ""
        )
        for i, line in enumerate(lines)
    ]


def expert_to_skill(
    expert_name: str, system_instructions: str, step_lines: List[str]
) -> Dict[str, Any]:
    """Build a draft skills row from one expert/packet-type's already-parsed
    workflow.

    Refuses (ValueError) only when a `[function:...]` marker names a
    handler with no registered `StepContract` (Task 7.1) -- see
    `_unconvertible_function_handlers`. This REPLACES the old blanket "any
    `[function:...]` marker at all" refusal: Phases 1-6 of the 2026-08-20
    plan built exactly the machinery (contracts, tool schema, mock mode,
    permission gating) that made that blanket refusal unnecessary for a
    contract-bearing handler.

    A `required_permission` on a handler's contract does NOT refuse
    conversion. That is a RUNTIME, per-caller check (Phase 6,
    `step_tool_schema.caller_holds_permission`), re-evaluated on every
    actual tool call against whoever is actually running the skill -- not a
    fixed property this one-off, caller-less script could usefully
    pre-judge at conversion time. `main()` still surfaces it in the printed
    dry-run output so whoever reviews the draft before `--apply` knows.
    """
    unconvertible = _unconvertible_function_handlers(step_lines)
    if unconvertible:
        plural = len(unconvertible) != 1
        raise ValueError(
            f"'{expert_name}' names function step(s) with no registered StepContract "
            f"({', '.join(unconvertible)}) -- converting would produce a step Phase 4's "
            f"tool machinery can't run or validate. Add a StepContract for "
            f"{'them' if plural else 'it'} first."
        )
    steps = build_steps_from_workflow_lines(step_lines, preamble=system_instructions)
    if not steps:
        raise ValueError(f"'{expert_name}' has no workflow steps to convert")
    title = expert_name.replace("_", " ").title()
    return {
        "slug": expert_name.replace("_", "-"),
        "title": title,
        "summary": f"Converted from the {title} expert. Review before activating.",
        "steps": steps,
        "inputs": [],
        "staff_only": True,
        "status": "draft",
    }


def _describe_step_for_preview(step: Dict[str, Any]) -> str:
    """One printable line for main()'s dry-run/apply step listing."""
    preview = (step.get("instruction") or "")[:200].replace("\n", " ")
    prefix = f"[function:{step['handler']}] " if step.get("kind") == "function" else ""
    marker = " [mutates, mock=ON by default]" if step.get("mutates") else ""
    return f"  {step['index'] + 1}. {prefix}{preview}{marker}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "expert",
        help="Expert id, matching '# Expert: <id>' in experts.definitions (e.g. grid_analyst)",
    )
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    from shared.prompts import PROMPTS

    body, source, _version = PROMPTS.resolve("experts.definitions")
    print(f"experts.definitions resolved from {source.value}, {len(body)} chars\n")

    # Real parsing, not a second reimplementation of it -- see this module's
    # docstring ("Phase 11 fix"). Uses the same ExpertInstructionsProvider
    # (and therefore the same cached PROMPTS.resolve("experts.definitions")
    # call above) real expert execution uses, so "what this script converts"
    # and "what the expert actually runs" can never drift apart again.
    from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider

    config = asyncio.run(ExpertInstructionsProvider().get_expert_config(args.expert))
    if config is None:
        print(f"No '# Expert: {args.expert}' section found.", file=sys.stderr)
        return 1

    if not config.workflows:
        print(
            f"'{args.expert}' has no '## Packet: <type>' / '### Workflow' section to "
            "convert -- nothing to do.",
            file=sys.stderr,
        )
        return 1
    if len(config.workflows) > 1:
        print(
            f"'{args.expert}' has {len(config.workflows)} packet types "
            f"({', '.join(config.workflows)}) -- converting more than one into a single "
            "flat skill isn't supported yet. Convert manually or extend this script.",
            file=sys.stderr,
        )
        return 1
    ((packet_type, step_lines),) = config.workflows.items()

    try:
        skill = expert_to_skill(args.expert, config.system_instructions, step_lines)
    except ValueError as e:
        print(f"Cannot convert: {e}", file=sys.stderr)
        return 1

    print(
        f"{skill['title']} -> {len(skill['steps'])} step(s), status={skill['status']} "
        f"(packet type: {packet_type})\n"
    )
    for step in skill["steps"]:
        print(_describe_step_for_preview(step))

    from orchestrator.experts.step_registry import get_step_contract

    permission_notes = [
        f"  - '{step['handler']}' requires {contract.required_permission!r}"
        for step in skill["steps"]
        if step.get("kind") == "function"
        and (contract := get_step_contract(step["handler"])) is not None
        and contract.required_permission
    ]
    if permission_notes:
        print("\nPermission-gated step(s) -- checked per-caller at run time, not blocked here:")
        print("\n".join(permission_notes))

    if not args.apply:
        print("\nDry run. Re-run with --apply to create the draft skill.")
        print(
            "Passing --apply is not itself a safety check -- read the step text above "
            "first. See this module's docstring for two ways a 'no unconvertible "
            "[function:...] markers' expert can still be a bad candidate."
        )
        return 0

    from supabase import create_client

    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    client = create_client(chat_db_url(), chat_db_service_key())
    skill["created_by"] = "convert_expert_to_skill.py"
    client.table("skills").insert(skill).execute()
    print(
        f"\nCreated draft skill '{skill['slug']}'. Review it in /skills, promote to "
        f"active, verify, then strike through '# Expert: {args.expert}' in the "
        f"experts.definitions source."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
