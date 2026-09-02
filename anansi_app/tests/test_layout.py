from nicegui_app import layout


def test_operations_nav_uses_requested_icons():
    labels_by_target = dict(layout.OPERATIONS_NAV)

    assert labels_by_target["/runs"] == "🎰 Runs"
    assert labels_by_target["/workflows"] == "🎬 Workflows"


def test_chats_url_stub_stays_at_conversations():
    """/chats (and /chat) can't be used here: DO ingress prefix-routes any
    path starting with "/chat" to chat-orchestrator's Telegram webhook
    before it ever reaches this app -- see .do/app.example.yaml's ingress
    rules and the matching comment on main.py's /conversations route. This
    is the one nav item whose URL stub can't be made to match its label.
    """
    labels_by_target = dict(layout.OPERATIONS_NAV)

    assert labels_by_target["/conversations"] == "💬 Chats"
    assert "/chats" not in labels_by_target
    assert "/chat" not in labels_by_target


def test_rag_knowledgebase_is_the_context_child():
    items = layout.bot_admin_nav_items()
    context_index = next(i for i, item in enumerate(items) if item.target == "/context")

    assert items[context_index].depth == 0
    assert items[context_index + 1].target == "/rag-knowledgebase"
    assert items[context_index + 1].label == "📚 RAG Knowledgebase"
    assert items[context_index + 1].depth == 1


def test_frame_mounts_the_chat_widget():
    """Static check: `frame` is the single mount point, so the widget appears
    on every admin page. Rendering it needs a NiceGUI runtime, which this
    suite does not have (conftest fakes `nicegui`), so assert on the source.
    """
    import ast
    import inspect

    from nicegui_app import layout

    source = inspect.getsource(layout.frame)
    calls = [
        node
        for node in ast.walk(ast.parse(source.lstrip()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mount"
    ]
    assert calls, "layout.frame() must call chat_widget.mount(user)"
