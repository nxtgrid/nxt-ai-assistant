"""What the current NiceGUI page is showing, published as chat context.

Each page calls ``set_page_context()`` as it renders; the chat widget
(``chat_widget.py``) reads it at send time and projects it through
``to_entity_context()`` onto chat_orchestrator's existing ``entity_context``
request field. The orchestrator side needs no change for this: the graph
already renders that field into an "[Entity Context]" block prepended to the
user turn (``conversation_graph._format_entity_context``).

Deliberately summary-only. The block carries identifiers plus a handful of
human-readable lines and one ``detail_hint`` naming how to go deeper; the bot
then drills in with the ticket/grid tools it already has. Shipping whole rows
would spend tokens on data the model can fetch on demand.

``anansi_app`` cannot import ``EntityContext`` (its image ships no
``orchestrator`` package -- see tests/test_no_orchestrator_imports.py), so the
projection returns a plain dict shaped like that model.

``from nicegui import app`` is deliberately function-local in the two storage
helpers below: anansi_app's test conftest fakes ``nicegui`` as a
``SimpleNamespace(run=..., ui=...)`` with no ``app`` attribute, so a
module-level import would break collection for the whole suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# One page load's worth of context. app.storage.client is discarded on reload
# AND on navigation, which is exactly the lifetime of "what this page shows".
_STORAGE_KEY = "anansi_chat_page_context"

MAX_SUMMARY_LINES = 12
MAX_SUMMARY_CHARS = 2000
MAX_SELECTION_CHARS = 4000
MAX_CHIP_CHARS = 40

# EntityContext's typed id slots (orchestrator/models/schemas.py). An
# identifier whose key matches one of these rides in the typed slot, where
# _format_entity_context gives it a stable label; anything else goes into
# additional_context under a humanised key.
_TYPED_ID_KEYS = frozenset(
    {"customer_id", "meter_id", "grid_id", "site_id", "installation_id"}
)


def _humanise(key: str) -> str:
    return key.replace("_", " ").capitalize()


@dataclass(frozen=True)
class PageContext:
    """One page's data item, as the chat widget will describe it to the model.

    ``kind``          machine-ish discriminator, e.g. "ticket", "ticket_list".
    ``label``         what the context chip says, e.g. "Ticket OPS-1".
    ``identifiers``   ids the model can feed to a tool. Typed keys (see
                      _TYPED_ID_KEYS) are promoted to EntityContext's own
                      fields; the rest are published as named lines.
    ``summary_lines`` short human lines, capped. Never whole rows.
    ``detail_hint``   one line naming how to get more, e.g. which tool to call.
    """

    kind: str
    label: str
    identifiers: Dict[str, str] = field(default_factory=dict)
    summary_lines: List[str] = field(default_factory=list)
    detail_hint: str = ""

    def chip_label(self) -> str:
        if len(self.label) <= MAX_CHIP_CHARS:
            return self.label
        return self.label[: MAX_CHIP_CHARS - 1] + "…"

    def summary_text(self) -> str:
        lines = [line.strip() for line in self.summary_lines if line and line.strip()]
        return "\n".join(lines[:MAX_SUMMARY_LINES])[:MAX_SUMMARY_CHARS]


def to_entity_context(
    page: Optional[PageContext], selection: str = ""
) -> Optional[Dict[str, Any]]:
    """Project page context + highlighted text onto an EntityContext-shaped dict.

    Returns None when there is nothing to attach, so the caller can omit the
    field entirely rather than sending an empty block.
    """
    typed: Dict[str, str] = {}
    extra: Dict[str, str] = {}

    if page is not None:
        extra["Page"] = page.label
        extra["Page type"] = page.kind
        for key, value in page.identifiers.items():
            if value in (None, ""):
                continue
            if key in _TYPED_ID_KEYS:
                typed[key] = str(value)
            else:
                extra[_humanise(key)] = str(value)
        summary = page.summary_text()
        if summary:
            extra["Page summary"] = summary
        if page.detail_hint:
            extra["To go deeper"] = page.detail_hint

    clean_selection = (selection or "").strip()
    if clean_selection:
        extra["Highlighted text"] = clean_selection[:MAX_SELECTION_CHARS]

    if not typed and not extra:
        return None

    payload: Dict[str, Any] = dict(typed)
    payload["additional_context"] = extra
    return payload


def set_page_context(page: Optional[PageContext]) -> None:
    """Publish what this page is showing. Safe to call outside a client context."""
    try:
        from nicegui import app

        app.storage.client[_STORAGE_KEY] = page
    except Exception:  # no client context (tests, background tasks) -- not fatal
        return


def get_page_context() -> Optional[PageContext]:
    """What the current page published, or None."""
    try:
        from nicegui import app

        value = app.storage.client.get(_STORAGE_KEY)
    except Exception:
        return None
    return value if isinstance(value, PageContext) else None
