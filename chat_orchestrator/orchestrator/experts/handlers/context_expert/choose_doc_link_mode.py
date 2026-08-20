"""Ask whether a Drive source should be linked live or copied in once.

A live link is the default and the point of the feature: the module reads
the document at request time, so edits to the document take effect without
anyone touching the module. A copy is a point-in-time snapshot that will
drift -- offered only because sometimes that is genuinely what you want.

A live link also skips improve_content: rewriting text that is discarded at
render time is waste, and the rewrite would misrepresent the document.
"""

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LIVE_WORDS = {"live", "link", "linked", "1", "yes"}
COPY_WORDS = {"copy", "snapshot", "text", "2", "no"}

QUESTION = (
    "Do you want this to stay linked to the document?\n\n"
    "1. **Link it live** — the bot re-reads the document each time, so your "
    "edits take effect automatically. Only people who can open the document "
    "will see its content.\n"
    "2. **Copy the text in now** — a snapshot. Later edits to the document "
    "won't reach the bot.\n\n"
    "Reply `1` or `2`."
)


@register_step("choose_doc_link_mode")
async def choose_doc_link_mode(context: StepContext) -> StepResult:
    """Ask once, for a Drive source only. Pasted text passes straight through."""
    if context.get_state("source_type") != "gdrive":
        return StepResult()

    if not context.get_state("awaiting_link_mode"):
        return StepResult(
            state_updates={"awaiting_link_mode": True},
            needs_user_input=True,
            user_prompt=QUESTION,
        )

    answer = (context.user_input or "").strip().lower()
    if answer in COPY_WORDS:
        LOGGER.info("Doc will be copied in as a snapshot")
        return StepResult(
            state_updates={"module_source": "manual", "awaiting_link_mode": False},
            progress_message="Copying the text in as a snapshot.",
        )
    if answer in LIVE_WORDS:
        LOGGER.info(f"Doc {context.get_state('source_id')} will be linked live")
        return StepResult(
            state_updates={
                "module_source": "gdoc",
                "module_source_ref": context.get_state("source_id") or "",
                "module_source_tab": "",
                "module_doc_audience": "acl_mirror",
                "skip_improve_content": True,
                "awaiting_link_mode": False,
            },
            progress_message="Linking the document live.",
        )

    return StepResult(needs_user_input=True, user_prompt=f"Please reply 1 or 2.\n\n{QUESTION}")
