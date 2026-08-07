"""Cached accessors for the (Streamlit-free) service layer.

The ``services/`` modules are plain Python and carry over from the Streamlit app
unchanged. NiceGUI runs an asyncio event loop, so callers should wrap the sync
methods here in ``nicegui.run.io_bound`` to avoid blocking it.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def get_reader():
    """Return a process-wide ``SupabaseReader`` (replaces st.cache_resource)."""
    from services.supabase_reader import SupabaseReader

    return SupabaseReader()


@lru_cache(maxsize=1)
def get_settings_service():
    """Return a process-wide ``SettingsService``."""
    from services.settings_service import SettingsService

    return SettingsService()


@lru_cache(maxsize=1)
def get_skill_builder_service():
    """Return a process-wide ``SkillBuilderService``."""
    from services.skill_builder_service import SkillBuilderService

    return SkillBuilderService()
