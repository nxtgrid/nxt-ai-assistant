# Expert Workflows

Expert workflows handle complex, multi-step operations that go beyond a single LLM call — things like "generate a preliminary design package" which requires fetching data, running a layout engine, creating a document, and summarising the result. Each step can be an LLM call or a Python function, and workflows can pause mid-run to ask the user for input.

## Concepts

- **Expert** — a named workflow engine defined in a Google Doc (`EXPERT_INSTRUCTIONS_DOC_ID`). Each expert has system instructions, a list of tools, and one or more packet types.
- **Packet** — one workflow run. Created when a slash command is invoked. Persisted in `agent_work_packets` so it survives restarts and can be retried.
- **Step** — a single unit of work. Either `[llm]` (calls Gemini) or `[function:handler_name]` (calls a registered Python function).
- **StepContext** — the object passed to every function step. Gives access to inputs, prior step results, persisted state, the MCP executor, and the requesting user's email.

## How a Request Flows

```
User: /lpp ExampleGrid
        │
        ▼
command_parser.py       ← looks up CommandDefinition, sets packet_type
        │
        ▼
expert_router.py        ← creates work packet in DB, loads expert config
        │
        ▼
workflow_executor.py    ← iterates steps in order
    ├── [llm] parse_request      → Gemini extracts site_name from message
    ├── [function:generate_map]  → Python: fetches DB data, runs layout engine
    ├── [function:copy_template] → Python: creates Google Doc from template
    └── [llm] summarize_result   → Gemini writes response to user
        │
        ▼
Response sent to user via Telegram
```

## Defining an Expert (Google Doc format)

Expert definitions live in the Google Doc at `EXPERT_INSTRUCTIONS_DOC_ID`. The parser expects this exact heading structure (use Shift+Enter for line breaks within a section to avoid Google Docs merging lines):

```
# Expert: my_expert_id

## System Instructions
You are a specialist in ...

## Tools
- customer_get_grid_status

## Packet Types
- my_packet_type

## Packet: my_packet_type

### Workflow
[llm] parse_inputs - Extract site name and date range from the user's message
[function:fetch_data] - Fetch raw data from the database
[llm] analyse_and_respond - Analyse the data and write the response

### Inputs
site_name: str - The grid site name extracted from the user message

### State
fetched_data_drive_id: str - Google Drive ID of the uploaded data file

### Outputs
summary: str - Human-readable summary sent to the user
```

## Writing a Step Handler

1. **Create the file** in `chat_orchestrator/orchestrator/experts/handlers/<expert_id>/my_step.py`:

```python
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step


@register_step("fetch_data")  # must match [function:fetch_data] in the workflow
async def fetch_data(context: StepContext) -> StepResult:
    site_name = context.get_input("site_name")
    
    # Use MCP tools via the executor
    result = await context.mcp_executor.call_tool(
        "customer_get_grid_status", {"grid_name": site_name}
    )
    
    return StepResult(
        data={"raw": result},
        progress_message=f"Fetched data for {site_name}",
    )
```

2. **Import the module** in `chat_orchestrator/orchestrator/experts/handlers/__init__.py`:

```python
from . import my_expert  # triggers @register_step decorators
```

3. **Register the slash command** (if needed) in `orchestrator/services/command_registry.py`:

```python
CommandDefinition(
    command="mycommand",
    command_type="expert",
    packet_type="my_packet_type",
    description="Short description shown in /help",
    requires_args=True,
    staff_only=True,
)
```

## StepResult Reference

```python
# Success with data
StepResult(
    data={"key": "value"},           # available to subsequent steps via get_previous_result()
    state_updates={"drive_id": "x"}, # persisted to DB, survives restarts
    progress_message="Done X",       # sent to user immediately (for long steps)
)

# Pause and ask user a question
StepResult.needs_input(
    "Which date range should I use?",
    state_updates={"awaiting_date": True},
)

# Failure
StepResult.failure("Could not find site — check the name and try again.")

# Cancel remaining steps
StepResult(skip_remaining=True)
```

## Handling User Input Mid-Workflow

When a step returns `StepResult.needs_input(...)`, the workflow pauses. On the next message from the user, execution resumes from the same step with `context.user_input` populated.

**Always check for cancel words first:**

```python
CANCEL_WORDS = {"cancel", "skip", "abort", "quit", "exit", "stop", "no"}

async def my_step(context: StepContext) -> StepResult:
    if context.user_input is not None:
        if context.user_input.strip().lower() in CANCEL_WORDS:
            return StepResult(skip_remaining=True)
        # process the answer ...
    
    if not context.get_state("awaiting_answer"):
        return StepResult.needs_input(
            "How many buildings?",
            state_updates={"awaiting_answer": True},
        )
```

## Storing Large Outputs

Never store large blobs (base64 images, raw data) in `state_updates` — they go into Supabase JSONB and will hit `statement_timeout`. Upload to Google Drive and store the file ID instead:

```python
from shared.utils.drive_upload import upload_step_output

drive_ids = await upload_step_output(
    site_folder_id=context.get_state("site_folder_id"),
    subfolder_name="Maps",
    site_name=site_name,
    files=[(image_bytes, "image/png", "layout_map")],
)

return StepResult(
    data={"image_b64": image_b64},          # full data in memory for this run
    state_updates={"map_drive_id": drive_ids["layout_map"]},  # ID only in DB
)
```

## Testing a Workflow Locally

```bash
cd chat_orchestrator && source .venv/bin/activate

python -c "
import asyncio
from orchestrator.experts.workflow_executor import WorkflowExecutor

async def test():
    executor = WorkflowExecutor()
    # ... build a mock packet and call executor.execute()

asyncio.run(test())
"
```

See `tests/experts/test_workflow_executor.py` and `tests/experts/conftest.py` for fixture patterns.
