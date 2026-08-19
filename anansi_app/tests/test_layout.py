from nicegui_app import layout


def test_rag_knowledgebase_is_the_context_child():
    items = layout.bot_admin_nav_items()
    context_index = next(i for i, item in enumerate(items) if item.target == "/knowledge-modules")

    assert items[context_index].depth == 0
    assert items[context_index + 1].target == "/documents"
    assert items[context_index + 1].label == "📚 RAG Knowledgebase"
    assert items[context_index + 1].depth == 1
