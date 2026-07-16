"""Bot Settings page (NiceGUI port of components/settings_page.py).

Registry-driven redesign: instead of hand-coding ~1,300 lines of bespoke
Streamlit widgets, every tunable flag is rendered generically from
``shared.config.flag_registry`` (the single source of truth for name, type,
default, editability). Widgets are chosen by ``FlagType``; read-only flags are
shown disabled; flags are grouped into ordered sections by a small rule table
with an "Other" catch-all so nothing is ever dropped.

Persistence reuses ``services.settings_service.SettingsService`` unchanged
(which wraps ``shared.config.settings_backends`` -> DigitalOcean / env-file).

Save model: widgets are event-driven, writing to a local ``pending`` dict with a
live "dirty" indicator (no Streamlit-style full rerun). A single explicit **Save**
persists all changes at once — deliberately *not* an autosave-per-keystroke,
because the DigitalOcean backend triggers a redeploy on write and per-change
saves would thrash it. If any changed flag is restart-scoped the button becomes
**Save & Restart**.
"""

from __future__ import annotations

import json
from typing import Any

from nicegui import run, ui

from nicegui_app.services_access import get_settings_service
from shared.config import flag_registry as registry
from shared.config.flag_registry import FlagType

# Restart-scoped flags (read once at process startup — see the Streamlit page).
RESTART_REQUIRED_KEYS = frozenset(
    {
        "PERSISTENT_AGENTS_ENABLED",
        "METRICS_ENABLED",
        "METRICS_SCHEDULE_HOUR",
        "GRAFANA_SYNC_HOUR",
        "MINI_APP_FORMS_ENABLED",
    }
)

# MCP server enable flags (grouped separately from feature toggles).
_MCP_SERVER_KEYS = frozenset(
    {
        "EQUIPMENT_DIAGNOSTICS_ENABLED",
        "VRM_ENABLED",
        "JIRA_ENABLED",
        "CODEBASE_ENABLED",
        "LOGS_ENABLED",
        "METERS_ENABLED",
        "EQUIPMENT_CONTROL_ENABLED",
        "PAYMENT_PROCESSOR_ENABLED",
        "CUSTOMER_ENABLED",
        "GRAFANA_ENABLED",
        "SCHEDULE_ENABLED",
        "META_ENABLED",
        "GRID_DESIGN_ENABLED",
        "SOLAR_ENABLED",
        "KNOWLEDGE_ENABLED",
        "MESSAGING_ENABLED",
        "REFERENCE_ENABLED",
    }
)


def _section_of(name: str) -> str:
    """Assign a flag to an ordered section (first match wins)."""
    if name == "BOT_ENABLED":
        return "🔴 Master Bot Control"
    if name in _MCP_SERVER_KEYS or name == "MCP_DISABLED_TOOLS":
        return "🔌 MCP Servers & Tools"
    if name == "LLM_PROVIDER" or name.startswith("GEMINI_") or name.startswith("OPENROUTER_") or name in (
        "EMBEDDING_MODEL",
        "VERIFICATION_MODEL",
        "INTENT_ROUTER_MODEL",
    ):
        return "🧠 Models"
    if name.startswith("GRAFANA_"):
        return "📊 Grafana"
    if name.startswith("LAYOUT_"):
        return "🗺️ Layout Engine"
    if name.startswith("rag__") or name in ("KNOWLEDGE_ENABLED", "REFERENCE_ENABLED"):
        return "📚 Knowledge Base (RAG)"
    if name.startswith("METRICS_"):
        return "📈 Metrics & Scheduling"
    if "ALLOWED" in name or "EDITORS" in name or "TELEGRAM_CHAT_ID" in name or "DOC_ID" in name:
        return "🔐 Access Control & Docs"
    return "🤖 Bot Behavior & Core"


_SECTION_ORDER = [
    "🔴 Master Bot Control",
    "🤖 Bot Behavior & Core",
    "🧠 Models",
    "🔌 MCP Servers & Tools",
    "📚 Knowledge Base (RAG)",
    "📊 Grafana",
    "📈 Metrics & Scheduling",
    "🗺️ Layout Engine",
    "🔐 Access Control & Docs",
    "🗂️ Other",
]


def _coerce_for_save(flag, value: Any) -> Any:
    if flag.type is FlagType.BOOL:
        return bool(value)
    if flag.type is FlagType.INT:
        return int(value) if value not in (None, "") else 0
    if flag.type is FlagType.FLOAT:
        return float(value) if value not in (None, "") else 0.0
    return "" if value is None else str(value)


def _model_select_options(svc, current: dict[str, Any]) -> dict[str, Any]:
    """Build provider/model select options without mixing provider-specific ids."""
    gemini_models = svc.get_gemini_models()
    openrouter_models = svc.get_openrouter_models()
    selected_openrouter_model = str(
        current.get("OPENROUTER_MODEL") or (openrouter_models[0] if openrouter_models else "")
    )
    provider_routes = svc.get_openrouter_provider_routes(selected_openrouter_model)
    return {
        "LLM_PROVIDER": svc.get_llm_provider_options(),
        "GEMINI_MODEL": gemini_models,
        "GEMINI_FALLBACK_MODEL": gemini_models,
        "GEMINI_DEEP_THINKING_MODEL": ["", *gemini_models],
        "INTENT_ROUTER_MODEL": gemini_models,
        "VERIFICATION_MODEL": gemini_models,
        "OPENROUTER_MODEL": openrouter_models,
        "OPENROUTER_PROVIDER_ORDER": provider_routes,
    }


def _options_with_current(options: Any, value: Any) -> Any:
    """Preserve the current value even if live model fetching omits it."""
    if isinstance(options, dict):
        if value in (None, "") or value in options:
            return options
        return {str(value): str(value), **options}
    values = [str(option) for option in options]
    if value not in (None, "") and str(value) not in values:
        values.insert(0, str(value))
    return values


async def render(log_levels: list[str] | None = None) -> None:
    ui.label("⚙️ Bot Settings").classes("text-h5")
    ui.label("Configure Anansi bot behavior and features.").classes("text-caption")

    svc = get_settings_service()
    current: dict[str, Any] = await run.io_bound(
        lambda: svc.get_current_settings(fetch_from_do=True)
    )
    log_levels = log_levels or svc.get_log_levels()

    pending: dict[str, Any] = dict(current)
    model_options: dict[str, Any] = await run.io_bound(lambda: _model_select_options(svc, current))

    # Group flag names by section.
    sections: dict[str, list[str]] = {title: [] for title in _SECTION_ORDER}
    for name in current:
        flag = registry.FLAGS.get(name)
        if flag is None or not flag.show_in_settings or flag.secret:
            continue
        sections.setdefault(_section_of(name), []).append(name)

    # Dirty-state footer (declared first so widget handlers can refresh it).
    save_bar = (
        ui.row()
        .classes("items-center gap-3 w-full q-pa-sm")
        .style("position: sticky; bottom: 0; background: rgba(20,24,36,0.06); border-radius: 8px")
    )

    def _changed() -> dict[str, Any]:
        out = {}
        for name, val in pending.items():
            flag = registry.FLAGS.get(name)
            if flag is None or not flag.editable:
                continue
            if _coerce_for_save(flag, val) != _coerce_for_save(flag, current.get(name)):
                out[name] = _coerce_for_save(flag, val)
        return out

    def _refresh_bar() -> None:
        save_bar.clear()
        changed = _changed()
        needs_restart = any(k in RESTART_REQUIRED_KEYS for k in changed)
        with save_bar:
            if not changed:
                ui.label("No unsaved changes.").classes("text-caption")
                return
            ui.label(f"{len(changed)} unsaved change(s).").classes("text-bold")
            if needs_restart:
                ui.label("Includes restart-scoped flags.").classes("text-warning text-caption")
            ui.space()
            ui.button("Discard", on_click=_discard).props("flat")
            ui.button(
                "Save & Restart" if needs_restart else "Save",
                on_click=lambda: _save(changed, needs_restart),
            ).props("color=primary")

    async def _save(changed: dict[str, Any], restart: bool) -> None:
        # Validate JSON flags before writing.
        for name, val in changed.items():
            if registry.FLAGS[name].type is FlagType.JSON:
                try:
                    json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    ui.notify(f"{name}: invalid JSON — not saved.", type="negative")
                    return
        ok, err = await run.io_bound(lambda: svc.update_settings(changed, restart_bot=restart))
        if ok:
            current.update(changed)
            # Panel enablement must also propagate to Supabase so the MCP server
            # hot-reloads the new selection on its next tool listing — no full
            # Grafana sync needed (mirrors the Streamlit page).
            if "GRAFANA_ENABLED_PANELS" in changed:
                await run.io_bound(
                    lambda: _sync_enabled_panels_to_supabase(changed["GRAFANA_ENABLED_PANELS"])
                )
            ui.notify("Saved. Bot restarting…" if restart else "Saved.", type="positive")
            _refresh_bar()
        else:
            ui.notify(f"Save failed: {err or 'unknown error'}", type="negative")

    def _discard() -> None:
        ui.navigate.reload()

    def _on_change(name: str, value: Any) -> None:
        pending[name] = value
        _refresh_bar()

    # Grafana dashboard/panel catalogue (from Supabase) powers the chip pickers.
    # Loaded once; falls back to empty (raw text fields) if the DB is unreachable.
    grafana_dashboards: dict[str, str] = {}
    grafana_panels: dict[str, dict] = {}
    if sections.get("📊 Grafana"):
        try:
            from services.grafana_metadata_service import (
                load_available_dashboards,
                load_panels_metadata,
            )

            grafana_dashboards = await run.io_bound(load_available_dashboards) or {}
            grafana_panels = await run.io_bound(load_panels_metadata) or {}
        except Exception:  # noqa: BLE001 — degrade to raw-field editing, never break the page
            grafana_dashboards, grafana_panels = {}, {}

    # Render sections in order. header-class/expand-icon-class fall through to
    # the underlying q-expansion-item (Quasar) root — the default header text is
    # small body copy, which reads as a plain label rather than a dropdown.
    for title in _SECTION_ORDER:
        names = sections.get(title) or []
        if not names:
            continue
        expanded = title in ("🔴 Master Bot Control", "🤖 Bot Behavior & Core")
        section = ui.expansion(title, value=expanded).classes("w-full q-mb-sm")
        section.props(
            'header-class="text-h6 text-weight-bold" expand-icon-class="text-h5" '
            "dense-toggle switch-toggle-side"
        )
        with section:
            if title == "📊 Grafana":
                _render_grafana_section(
                    names,
                    pending,
                    log_levels,
                    model_options,
                    _on_change,
                    grafana_dashboards,
                    grafana_panels,
                )
            elif title == "🧠 Models":
                _render_models_section(names, pending, log_levels, model_options, _on_change)
            else:
                # Two-column grid: most flags are short (toggle/number/one-line
                # string) and read better side-by-side. JSON textareas opt out
                # of the column span (see _render_flag) since they're long.
                with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-0"):
                    for name in names:
                        _render_flag(name, pending, log_levels, model_options, _on_change)

    _refresh_bar()


def _csv_to_list(value: Any) -> list[str]:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _sync_enabled_panels_to_supabase(enabled_panels_str: str) -> None:
    """Push enabled panel selections to Supabase ``enabled_panel_ids``.

    Lets the MCP server hot-reload the new selection on its next tool listing
    without requiring a full Grafana sync. Every known dashboard is updated —
    including ones whose last panel was just deselected — so stale selections
    don't linger. Ported from the Streamlit settings page.
    """
    from services.grafana_metadata_service import (
        load_available_dashboards,
        update_enabled_panels,
    )

    by_dashboard: dict[str, list[str]] = {}
    for key in _csv_to_list(enabled_panels_str):
        uid, _, panel_id = key.partition(":")
        if uid and panel_id:
            by_dashboard.setdefault(uid, []).append(panel_id)

    for uid in load_available_dashboards():
        update_enabled_panels(uid, by_dashboard.get(uid, []))


def _render_models_section(
    names: list[str],
    pending: dict,
    log_levels: list[str],
    model_options: dict[str, Any],
    on_change,
) -> None:
    remaining = list(names)

    def _take(key: str) -> bool:
        if key in remaining:
            remaining.remove(key)
            return True
        return False

    provider_routes = model_options.get("OPENROUTER_PROVIDER_ORDER") or {}
    selected_model = pending.get("OPENROUTER_MODEL") or ""
    with ui.card().classes("w-full q-mb-md").style("grid-column: 1 / -1"):
        ui.label("OpenRouter provider route").classes("text-subtitle1 text-weight-bold")
        if provider_routes:
            ui.label(
                "Provider endpoint routes available for the selected OpenRouter model. "
                "If that provider has BYOK configured with “Always use for this provider,” "
                "OpenRouter will use it for the route."
            ).classes("text-caption").style("color: #64748b")
            for provider, label in provider_routes.items():
                ui.label(f"{provider} · {label}").classes("text-caption")
        else:
            ui.label(
                "No provider routes were discovered for the selected OpenRouter model. "
                "The picker still accepts the current value if one is configured."
            ).classes("text-caption").style("color: #64748b")
        if selected_model:
            ui.label(f"Routes shown for: {selected_model}").classes("text-caption").style(
                "color: #64748b"
            )

    with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-0"):
        for key in (
            "LLM_PROVIDER",
            "GEMINI_MODEL",
            "GEMINI_FALLBACK_MODEL",
            "OPENROUTER_MODEL",
            "OPENROUTER_PROVIDER_ORDER",
            "OPENROUTER_ALLOW_FALLBACKS",
        ):
            if _take(key):
                _render_flag(key, pending, log_levels, model_options, on_change)
        for name in remaining:
            _render_flag(name, pending, log_levels, model_options, on_change)


def _render_grafana_section(
    names: list[str],
    pending: dict,
    log_levels: list[str],
    model_options: dict[str, Any],
    on_change,
    available_dashboards: dict[str, str],
    panels_metadata: dict[str, dict],
) -> None:
    """Custom Grafana section: chip pickers for dashboards/panels, a sync-hour
    slider, a reindex toggle and a Sync Now button — a NiceGUI port of the
    previous Streamlit "Dashboard & Panel Selection" UI. Everything writes back
    through ``on_change`` into the shared ``pending`` dict, so the standard
    Save/Discard footer picks the edits up unchanged.
    """
    remaining = list(names)

    def _take(key: str) -> bool:
        if key in remaining:
            remaining.remove(key)
            return True
        return False

    # Plain text flags stay as normal inputs, rendered first.
    for key in (
        "GRAFANA_URL",
        "GRAFANA_USERNAME",
        "GRAFANA_FOLDER_NAME",
        "GRAFANA_PANEL_DESCRIPTION_PROMPT",
    ):
        if _take(key):
            _render_flag(key, pending, log_levels, model_options, on_change)

    # Machine-managed blobs are surfaced via the pickers below — hide the raw,
    # multi-kilobyte read-only textareas that used to clutter the section.
    _take("GRAFANA_PANELS_METADATA")
    _take("GRAFANA_AVAILABLE_DASHBOARDS")

    have_catalogue = bool(available_dashboards or panels_metadata)

    with ui.card().classes("w-full q-mt-sm q-mb-md"):
        ui.label("Dashboard & Panel Selection").classes("text-subtitle1 text-weight-bold")

        if not have_catalogue:
            # No synced catalogue yet — fall back to raw CSV editing so the
            # admin is never locked out, and point them at Sync Now.
            ui.label(
                "No dashboards indexed yet. Run “Sync Now” below to populate the "
                "picker, or edit the raw values here."
            ).classes("text-caption").style("color: #64748b")
            for key in ("GRAFANA_ENABLED_DASHBOARDS", "GRAFANA_ENABLED_PANELS"):
                if _take(key):
                    _render_flag(key, pending, log_levels, model_options, on_change)
        else:
            _take("GRAFANA_ENABLED_DASHBOARDS")
            _take("GRAFANA_ENABLED_PANELS")

            with ui.row().classes("w-full gap-4 no-wrap items-start"):
                dash_col = ui.column().classes("gap-1").style("flex: 1 1 0; min-width: 0")
                panel_col = ui.column().classes("gap-1").style("flex: 1 1 0; min-width: 0")

            enabled_dash = [
                d
                for d in _csv_to_list(pending.get("GRAFANA_ENABLED_DASHBOARDS"))
                if d in available_dashboards
            ]

            # Assigned below inside panel_col; the picker helpers close over it
            # and only dereference it at call time.
            panel_holder: Any = None

            def _panel_options(selected_uids: list[str]) -> dict[str, str]:
                sel = set(selected_uids)
                return {
                    key: f"{info.get('dashboard_title', 'Unknown')} — "
                    f"{info.get('title', 'Untitled')}"
                    for key, info in panels_metadata.items()
                    if info.get("dashboard_uid") in sel
                }

            def _rebuild_panels(selected_uids: list[str]) -> None:
                panel_holder.clear()
                options = _panel_options(selected_uids)
                valid = [
                    p for p in _csv_to_list(pending.get("GRAFANA_ENABLED_PANELS")) if p in options
                ]
                # Prune panels belonging to now-deselected dashboards.
                on_change("GRAFANA_ENABLED_PANELS", ",".join(valid))
                with panel_holder:
                    if not selected_uids:
                        ui.label("Select dashboards to choose their panels.").classes(
                            "text-caption"
                        ).style("color: #64748b")
                        return
                    if not options:
                        ui.label("No panels indexed for the selected dashboards.").classes(
                            "text-caption"
                        ).style("color: #64748b")
                        return
                    ui.select(
                        options=options,
                        value=valid,
                        multiple=True,
                        with_input=True,
                        on_change=lambda e: on_change(
                            "GRAFANA_ENABLED_PANELS", ",".join(e.value or [])
                        ),
                    ).props("use-chips outlined dense clearable").classes("w-full")

            with dash_col:
                ui.label("Enabled Dashboards").classes("text-caption text-weight-medium")

                def _on_dash_change(e) -> None:
                    on_change("GRAFANA_ENABLED_DASHBOARDS", ",".join(e.value or []))
                    _rebuild_panels(list(e.value or []))

                ui.select(
                    options=available_dashboards,
                    value=enabled_dash,
                    multiple=True,
                    with_input=True,
                    on_change=_on_dash_change,
                ).props("use-chips outlined dense clearable").classes("w-full")
                ui.label("Only panels from selected dashboards appear on the right.").classes(
                    "text-caption"
                ).style("color: #64748b")

            with panel_col:
                ui.label("Enabled Panels").classes("text-caption text-weight-medium")
                panel_holder = ui.column().classes("w-full")
                _rebuild_panels(enabled_dash)
                ui.label("Each enabled panel becomes an MCP tool.").classes("text-caption").style(
                    "color: #64748b"
                )

    # Sync hour slider (restart-scoped) + force-reindex toggle.
    if _take("GRAFANA_SYNC_HOUR"):
        hour = int(pending.get("GRAFANA_SYNC_HOUR") or 0)
        with ui.column().classes("gap-0 w-full q-mb-sm"):
            ui.label(f"Nightly Sync Hour (UTC): {hour:02d}:00").classes(
                "text-caption text-weight-medium"
            ).bind_text_from(
                pending,
                "GRAFANA_SYNC_HOUR",
                backward=lambda v: f"Nightly Sync Hour (UTC): {int(v or 0):02d}:00",
            )
            ui.slider(
                min=0,
                max=23,
                value=hour,
                on_change=lambda e: on_change("GRAFANA_SYNC_HOUR", int(e.value)),
            ).props("label-always").classes("w-full")
            ui.label("Hour of day to run automatic panel indexing (restart required).").classes(
                "text-caption"
            ).style("color: #64748b")

    if _take("GRAFANA_FORCE_FULL_REINDEX"):
        ui.switch(
            "Force Full Reindex",
            value=bool(pending.get("GRAFANA_FORCE_FULL_REINDEX")),
            on_change=lambda e: on_change("GRAFANA_FORCE_FULL_REINDEX", e.value),
        )
        ui.label(
            "Next sync regenerates ALL panel descriptions (ignores caching). "
            "Disable once the sync completes."
        ).classes("text-caption").style("color: #64748b")

    # Sync Now — re-index dashboards/panels from Grafana (only needed when new
    # panels are added; enabling/disabling existing panels is instant via Save).
    async def _sync_now() -> None:
        ui.notify("Starting Grafana sync — this can take a minute…")
        try:
            result = await run.io_bound(_run_grafana_indexer)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Sync failed to start: {exc}", type="negative")
            return
        if result.returncode == 0:
            ui.notify("Grafana sync complete. Reload the page to see new panels.", type="positive")
        else:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            ui.notify(
                "Grafana sync failed:\n" + "\n".join(tail),
                type="negative",
                multi_line=True,
                timeout=10000,
            )

    ui.button("🔄 Sync Now", on_click=_sync_now).props("outline color=primary").classes(
        "w-full q-mt-sm"
    )
    ui.label(
        "Re-index Grafana dashboards and generate panel descriptions. Only needed when "
        "new panels are added in Grafana — toggling existing panels just needs Save."
    ).classes("text-caption").style("color: #64748b")

    # Any remaining Grafana flags (future additions) render generically.
    for name in remaining:
        _render_flag(name, pending, log_levels, model_options, on_change)


def _run_grafana_indexer():
    """Run the incremental Grafana indexer script as a subprocess (blocking)."""
    import os
    import subprocess
    import sys

    script_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "grafana_indexer_incremental.py"
    )
    return subprocess.run(
        [sys.executable, script_path],
        capture_output=True,
        text=True,
        timeout=600,
    )


def _render_flag(
    name: str,
    pending: dict,
    log_levels: list[str],
    model_options: dict[str, Any],
    on_change,
) -> None:
    flag = registry.FLAGS[name]
    value = pending.get(name)
    disabled = not flag.editable
    label = f"{name}" + ("  (read-only)" if disabled else "")

    def handler(e, n=name) -> None:
        on_change(n, e.value)

    wrapper = ui.column().classes("gap-0 w-full q-mb-sm")
    if flag.type is FlagType.JSON:
        # JSON blobs (arrays/objects) tend to be long — span both grid columns
        # instead of squeezing a textarea into a half-width cell. A no-op
        # outside a grid container (e.g. the Grafana section's own layout).
        wrapper.style("grid-column: 1 / -1")
    with wrapper:
        w: Any
        if flag.type is FlagType.BOOL:
            w = ui.switch(label, value=bool(value), on_change=handler)
        elif flag.type is FlagType.INT:
            w = ui.number(label, value=value, precision=0, on_change=handler).classes("w-full")
        elif flag.type is FlagType.FLOAT:
            w = ui.number(label, value=value, on_change=handler).classes("w-full")
        elif flag.type is FlagType.JSON:
            w = ui.textarea(label, value=str(value or ""), on_change=handler).classes("w-full")
        elif name == "LOG_LEVEL":
            opts = log_levels or ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            w = ui.select(opts, value=value, label=label, on_change=handler).classes("w-full")
        elif name == "OPENROUTER_PROVIDER_ORDER":
            opts = _options_with_current(model_options.get(name, {}), value)
            current = _csv_to_list(value)
            if isinstance(opts, dict):
                for provider in current:
                    opts.setdefault(provider, provider)
            else:
                opts = _options_with_current(opts, value)
            w = (
                ui.select(
                    opts,
                    value=current,
                    label=label,
                    multiple=True,
                    with_input=True,
                    on_change=lambda e, n=name: on_change(n, ",".join(e.value or [])),
                )
                .props("use-chips outlined dense clearable")
                .classes("w-full")
            )
        elif name in model_options:
            opts = _options_with_current(model_options[name], value)
            w = (
                ui.select(opts, value=value, label=label, with_input=True, on_change=handler)
                .props("outlined dense clearable")
                .classes("w-full")
            )
        else:
            w = ui.input(label, value=str(value or ""), on_change=handler).classes("w-full")

        if disabled:
            w.disable()
        if flag.description:
            ui.label(flag.description).classes("text-caption").style("color: #64748b")
