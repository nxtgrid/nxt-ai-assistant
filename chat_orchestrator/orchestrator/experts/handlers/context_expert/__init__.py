"""Context Expert step handlers.

Authors curated context modules (`knowledge_modules`) from staff input, as
opposed to the ingestion_expert which embeds documents into the RAG corpus.

- propose_module: LLM drafts slug/title/summary from the improved body
- detect_module_duplicates: slug/hash/title collision check against existing modules
- select_prompts: ask which prompts should use this module
- prepare_module_approval: build the approval summary
- store_module: write knowledge_modules + prompt_knowledge_overrides

fetch_document and improve_content are reused unchanged from ingestion_expert --
step handlers register globally by name, so the workflow just names them.
"""

from orchestrator.experts.handlers.context_expert.propose_module import propose_module

__all__ = [
    "propose_module",
]
