"""Convert a prompt-only expert into a draft skill.

Phase 5, Task 14 of
docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md.

The plan named five experts as convertible on the premise that they are
"prompt-only... with no workflow at all": grid_analyst, grid_monitor,
site_visit_tracker, signing, community_sizing. Direct inspection of the live
experts.definitions doc (`python scripts/convert_expert_to_skill.py <name>`,
dry run, against each) found that premise wrong for all five:

- grid_analyst, signing, community_sizing each have real [function:...]
  workflow steps in their doc section (6, 1, and 2 respectively) -- pipeline
  experts exactly like context_expert/grids_technical_reviewer/
  ingestion_expert/package_generator, just not previously recognized as
  such. A plain text split cannot represent a registered-handler call, so
  converting one of these would silently drop that work rather than
  preserve it -- e.g. signing's actual e-signature request, or
  community_sizing's boundary detection.
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
the one thing this script can check mechanically: zero [function:...]
markers in the doc section (has_function_steps). That is necessary but not
sufficient -- it would not have caught site_visit_tracker's dead-scaffolding
problem, which has no textual signature to grep for -- so this still
requires a human to actually read the dry-run output's step text before
passing --apply, for any expert, not just trust a green exit code.

Everything that does convert lands as status='draft': reviewed and promoted
by a human, never activated by this script.

Usage:
    python scripts/convert_expert_to_skill.py grid_analyst
    python scripts/convert_expert_to_skill.py grid_analyst --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Dict, List

_NUMBERED = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)
_FUNCTION_MARKER_RE = re.compile(r"\[function:[a-zA-Z0-9_]+\]")


def has_function_steps(instructions: str) -> bool:
    """Whether this expert's doc section names any [function:handler] step.

    See this module's docstring: a pipeline expert must stay as code, and
    this marker is the one mechanical signal for "pipeline expert" this
    script can actually check, rather than trusting a hardcoded name list.
    """
    return bool(_FUNCTION_MARKER_RE.search(instructions or ""))


def split_instructions_into_steps(instructions: str) -> List[Dict[str, Any]]:
    """Split an expert's instruction block into skill steps.

    A numbered list becomes one step per item. Anything else becomes a
    single step -- which is still an improvement: the result is nameable,
    schedulable and editable without touching a Google Doc.

    Text before the first numbered item is the persona and is prepended to
    step one; dropping it would lose the expert's identity entirely.
    """
    text = (instructions or "").strip()
    if not text:
        return []

    matches = list(_NUMBERED.finditer(text))
    if not matches:
        return [
            {
                "index": 0,
                "name": "step_1",
                "instruction": text,
                "allow_write": False,
                "is_response_step": True,
            }
        ]

    preamble = text[: matches[0].start()].strip()
    steps: List[Dict[str, Any]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        body = _NUMBERED.sub(r"\2", body, count=1).strip()
        if i == 0 and preamble:
            body = f"{preamble}\n\n{body}"
        steps.append(
            {
                "index": i,
                "name": f"step_{i + 1}",
                "instruction": body,
                "allow_write": False,
                "is_response_step": i == len(matches) - 1,
            }
        )
    return steps


def expert_to_skill(expert_name: str, instructions: str) -> Dict[str, Any]:
    """Build a draft skills row from a prompt-only expert.

    Refuses (ValueError) an expert whose doc section names any
    [function:...] step -- see has_function_steps and this module's
    docstring. Does not otherwise restrict which expert_name may be
    converted: there is no reliable static allowlist (see docstring), so
    whoever runs this -- a human, or an agent on their behalf -- is the one
    actually deciding an expert is a good candidate, by reading its dry-run
    output first.
    """
    if has_function_steps(instructions):
        markers = sorted(set(_FUNCTION_MARKER_RE.findall(instructions)))
        raise ValueError(
            f"'{expert_name}' has function steps ({', '.join(markers)}) and stays as "
            f"code -- converting would silently drop that work."
        )
    steps = split_instructions_into_steps(instructions)
    if not steps:
        raise ValueError(f"'{expert_name}' has no instruction text to convert")
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

    section = re.search(
        rf"^# Expert: {re.escape(args.expert)}\s*$(.*?)(?=^# Expert: |\Z)",
        PROMPTS.text("experts.definitions"),
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        print(f"No '# Expert: {args.expert}' section found.", file=sys.stderr)
        return 1

    try:
        skill = expert_to_skill(args.expert, section.group(1))
    except ValueError as e:
        print(f"Cannot convert: {e}", file=sys.stderr)
        return 1

    print(f"{skill['title']} -> {len(skill['steps'])} step(s), status={skill['status']}\n")
    for step in skill["steps"]:
        preview = step["instruction"][:200].replace("\n", " ")
        print(f"  {step['index'] + 1}. {preview}...")

    if not args.apply:
        print("\nDry run. Re-run with --apply to create the draft skill.")
        print(
            "Passing --apply is not itself a safety check -- read the step text above "
            "first. See this module's docstring for two ways a 'no [function:...] "
            "markers' expert can still be a bad candidate."
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
