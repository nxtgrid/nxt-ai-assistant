"""Tests for the Tickets page module + routing (Task 8).

Follows this app's convention of faking ``nicegui`` at the sys.modules level
before importing a ``nicegui_app.pages.*`` module (there is no NiceGUI runtime
in tests). Covers the page's pure helpers, a static "no mutation controls"
guarantee, and AST-level checks of main.py's routing/RBAC/landing precedence.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.modules.setdefault(
    "nicegui",
    SimpleNamespace(run=SimpleNamespace(), ui=SimpleNamespace()),
)

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


def test_format_time_ago():
    assert tickets_page._format_time_ago(None) == "—"
    three_days = datetime.utcnow() - timedelta(days=3)
    assert tickets_page._format_time_ago(three_days.isoformat()) == "3d ago"
    assert tickets_page._format_time_ago(datetime.utcnow().isoformat()) == "just now"


# ── read-only guarantee: the page must call NO write paths ─────────────────────
def test_page_only_calls_readonly_reader_methods():
    src = open(_TICKETS_PATH).read()
    db_calls = set(re.findall(r"db\.(\w+)", src))
    assert db_calls <= {"is_configured", "list_tickets", "get_ticket_detail"}, db_calls


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


def test_bot_admin_nav_lists_tickets_first():
    from nicegui_app import layout

    assert layout.BOT_ADMIN_NAV[0] == ("/tickets", "🎫 Tickets")
