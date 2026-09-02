"""Tests for the Tickets page module + routing (Task 8).

Covers the page's pure helpers, a static "no mutation controls" guarantee,
and AST-level checks of main.py's routing/RBAC/landing precedence. (``nicegui``
is faked at the sys.modules level in conftest.py so ``nicegui_app.pages.*``
modules can be imported without a NiceGUI runtime.)
"""

from __future__ import annotations

import ast
import os
import re
from datetime import datetime, timedelta

from nicegui_app.pages import tickets as tickets_page

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAIN_PATH = os.path.join(_REPO_ROOT, "anansi_app", "nicegui_app", "main.py")
_TICKETS_PATH = os.path.join(_REPO_ROOT, "anansi_app", "nicegui_app", "pages", "tickets.py")


# ── pure helpers ───────────────────────────────────────────────────────────────
def test_build_telegram_msg_link_supergroup():
    assert (
        tickets_page._build_telegram_msg_link("-1001234567890", 42)
        == "https://t.me/c/1234567890/42"
    )


def test_build_telegram_msg_link_rejects_non_supergroup_and_missing():
    assert tickets_page._build_telegram_msg_link("123456", 42) is None
    assert tickets_page._build_telegram_msg_link("-1001234567890", None) is None
    assert tickets_page._build_telegram_msg_link("", 42) is None
    assert tickets_page._build_telegram_msg_link("-100abc", 42) is None


def test_mask_customer_prefers_username_then_masks_email_then_chat_id():
    assert tickets_page._mask_customer({"customer_username": "alice"}) == "@alice"
    assert tickets_page._mask_customer({"customer_username": "@bob"}) == "@bob"
    masked = tickets_page._mask_customer({"customer_email": "carol@example.com"})
    assert masked == "c***@example.com"
    assert tickets_page._mask_customer({"customer_chat_id": 987654}) == "user •••7654"
    assert tickets_page._mask_customer({}) == "unknown"


def test_backend_chip():
    assert tickets_page._backend_chip("jira") == "🎫 Jira"
    assert tickets_page._backend_chip("internal") == "🗂 Internal"


def test_ticket_origin_and_escalation_context_labels():
    assert tickets_page._origin_label("escalation") == "🆘 Customer escalation"
    assert tickets_page._origin_label("notification") == "🔔 Operational notification"
    assert tickets_page._origin_label("adopted") == "↗ Adopted"
    assert tickets_page._escalation_context_label(True) == "🆘 Escalation"
    assert tickets_page._escalation_context_label(False) == "🔔 No escalation"


def test_format_time_ago():
    assert tickets_page._format_time_ago(None) == "—"
    three_days = datetime.utcnow() - timedelta(days=3)
    assert tickets_page._format_time_ago(three_days.isoformat()) == "3d ago"
    assert tickets_page._format_time_ago(datetime.utcnow().isoformat()) == "just now"


def test_status_filter_defaults_to_open_and_in_progress():
    assert tickets_page._status_filter_value(tickets_page._DEFAULT_STATUS_FILTER) == [
        "open",
        "in_progress",
    ]


def test_status_filter_all_means_no_filter():
    assert tickets_page._status_filter_value("all") is None


def test_status_filter_passes_through_a_single_status():
    assert tickets_page._status_filter_value("done") == "done"


# ── default backend filter ──────────────────────────────────────────────────
_JIRA_ENV_VARS = ("JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN")


def _clear_jira_env(monkeypatch):
    monkeypatch.delenv("TICKET_BACKEND_OVERRIDE", raising=False)
    for var in _JIRA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_default_backend_filter_is_all_when_override_is_internal(monkeypatch):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "internal")

    assert tickets_page._default_backend_filter() == "all"


def test_default_backend_filter_is_jira_when_override_is_jira(monkeypatch):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "jira")

    assert tickets_page._default_backend_filter() == "jira"


def test_default_backend_filter_is_jira_in_auto_mode_when_credentials_are_configured(
    monkeypatch,
):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")
    for var in _JIRA_ENV_VARS:
        monkeypatch.setenv(var, "x")

    assert tickets_page._default_backend_filter() == "jira"


def test_default_backend_filter_is_all_in_auto_mode_without_credentials(monkeypatch):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")

    assert tickets_page._default_backend_filter() == "all"


def test_default_backend_filter_partial_credentials_are_not_enough(monkeypatch):
    """All three Jira connection vars must be present -- matches
    JiraTicketBackend.has_credentials()'s all-or-nothing gate that
    resolve_backend()'s "auto"/"jira" branches rely on."""
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "bot@example.com")
    # JIRA_API_TOKEN left unset.

    assert tickets_page._default_backend_filter() == "all"


# ── read-only guarantee: the page must call NO write paths ─────────────────────
def test_page_only_calls_readonly_reader_methods():
    src = open(_TICKETS_PATH).read()
    db_calls = set(re.findall(r"db\.(\w+)", src))
    assert db_calls <= {
        "is_configured", "list_ticket_page", "get_canonical_ticket_detail"
    }, db_calls


def test_page_uses_canonical_ticket_list_filters():
    src = open(_TICKETS_PATH).read()
    assert "db.list_ticket_page(" in src
    assert '"created_via"' in src
    assert '"has_escalation"' in src


def test_page_renders_only_recorded_delivery_links_with_purpose_labels():
    src = open(_TICKETS_PATH).read()
    assert "get_canonical_ticket_detail" in src
    assert "Notification message" in src
    assert "Escalation message" in src


def test_ticket_row_badges_read_real_ticket_list_view_columns():
    """affected_keys_count/comment_count were never real ticket_list_view
    columns (the view has affected_count/activity_count) -- both badges
    silently rendered 0 for every ticket. Guard against reintroducing
    either stale key."""
    src = open(_TICKETS_PATH).read()
    assert 'ticket.get("affected_keys_count")' not in src
    assert "ticket.get('comment_count'" not in src
    assert 'ticket.get("affected_count")' in src
    assert "ticket.get('activity_count'" in src


def test_page_has_no_mutation_control_tokens():
    src = open(_TICKETS_PATH).read()
    forbidden = [
        "add_comment",
        "update_internal_ticket",
        "close_ticket",
        "resolve_ticket",
        "tag_message",
        ".insert(",
        ".update(",
        ".delete(",
        "delete_",
    ]
    hits = [tok for tok in forbidden if tok in src]
    assert hits == [], f"mutation-capable tokens found on read-only page: {hits}"


# ── routing / RBAC / landing precedence (AST over main.py, no import) ──────────
def _main_tree() -> ast.Module:
    return ast.parse(open(_MAIN_PATH).read())


def _func(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in main.py")


def test_tickets_route_registered_and_rbac_gated():
    tree = _main_tree()
    route = _func(tree, "tickets_route")
    src = ast.get_source_segment(open(_MAIN_PATH).read(), route)
    assert "can_view_bot_admin" in src
    assert "access_denied" in src
    assert "tickets.render(user)" in src


def test_ticket_detail_route_passes_ref():
    tree = _main_tree()
    route = _func(tree, "ticket_detail_route")
    src = ast.get_source_segment(open(_MAIN_PATH).read(), route)
    assert "can_view_bot_admin" in src
    assert "tickets.render(user, ref)" in src


def test_index_page_lands_bot_admin_on_tickets_first():
    tree = _main_tree()
    index = _func(tree, "index_page")

    # Find the branch that lives inside the `with layout.frame(...)` block.
    with_node = next(n for n in ast.walk(index) if isinstance(n, ast.With))
    if_node = next(n for n in with_node.body if isinstance(n, ast.If))

    # First tested condition must be can_admin, and its body navigates to /tickets.
    assert isinstance(if_node.test, ast.Name) and if_node.test.id == "can_admin"
    admin_src = "\n".join(ast.get_source_segment(open(_MAIN_PATH).read(), s) for s in if_node.body)
    assert "/tickets" in admin_src

    # The grid welcome must be demoted to the elif branch.
    assert len(if_node.orelse) == 1
    elif_node = if_node.orelse[0]
    assert isinstance(elif_node, ast.If)
    assert isinstance(elif_node.test, ast.Name) and elif_node.test.id == "can_grid"


def test_operations_nav_lists_tickets_first():
    from nicegui_app import layout

    assert layout.OPERATIONS_NAV[0] == ("/tickets", "🎫 Tickets")


# ── chat page context ─────────────────────────────────────────────────────────
def test_ticket_page_context_carries_the_ref_as_an_identifier():
    page = tickets_page.build_ticket_page_context(
        {"ticket_ref": "REF-1", "summary": "Meter offline", "status": "open"}
    )
    assert page.kind == "ticket"
    assert page.label == "Ticket REF-1"
    assert page.identifiers["ticket_ref"] == "REF-1"


def test_ticket_page_context_summarises_without_leaking_the_customer():
    page = tickets_page.build_ticket_page_context(
        {
            "ticket_ref": "REF-1",
            "summary": "Meter offline",
            "status": "open",
            "backend": "internal",
            "customer_email": "someone@example.com",
            "grid_name": "Alpha",
        }
    )
    summary = page.summary_text()
    assert "Meter offline" in summary
    assert "Alpha" in summary
    # _mask_customer's output, never the raw address.
    assert "someone@example.com" not in summary
    assert "s***@example.com" in summary


def test_ticket_page_context_points_the_model_at_the_ticket_tools():
    page = tickets_page.build_ticket_page_context({"ticket_ref": "REF-1"})
    assert "ticket_ref" in page.detail_hint


def test_ticket_list_page_context_lists_refs_not_rows():
    tickets = [{"ticket_ref": f"REF-{i}", "summary": "x" * 500} for i in range(25)]
    page = tickets_page.build_ticket_list_page_context(tickets, total=93, status="open")
    assert page.kind == "ticket_list"
    assert page.label == "Tickets (93)"
    summary = page.summary_text()
    assert "REF-0" in summary
    # Capped at 10 refs, so the 11th is absent and no row body is included.
    assert "REF-11" not in summary
    assert "x" * 500 not in summary


def test_ticket_list_page_context_states_the_active_status_filter():
    page = tickets_page.build_ticket_list_page_context([], total=0, status="closed")
    assert "closed" in page.summary_text()


def test_tickets_page_publishes_context_on_both_views():
    """Static check: both the list and the single-ticket view must call
    set_page_context, or the chat widget silently has nothing attached."""
    source = open(_TICKETS_PATH).read()
    tree = ast.parse(source)
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "set_page_context"
            for inner in ast.walk(node)
        )
    }
    assert "_reload" in callers
    assert "_render_single_detail" in callers
