"""Bot Settings page (NiceGUI port of components/settings_page.py).

Registry-driven redesign: instead of hand-coding ~1,300 lines of bespoke
Streamlit widgets, every tunable flag is rendered generically from
``shared.config.flag_registry`` (the single source of truth for name, type,
default, group, label, choices, bounds and editability). Widgets are chosen by
``settings_widgets.render_mode``; sections come from ``registry.groups()`` so a
new flag needs no edit to this file.

Persistence reuses ``services.settings_service.SettingsService`` unchanged
(which wraps ``shared.config.settings_backends`` -> DigitalOcean, explicit
local env-file, or read-only fallback).

Save model: widgets are event-driven, writing to a local ``pending`` dict with a
live "dirty" indicator (no Streamlit-style full rerun). A single explicit **Save**
persists all changes at once — deliberately *not* an autosave-per-keystroke,
because the DigitalOcean backend triggers a redeploy on write and per-change
saves would thrash it. If any changed flag is restart-scoped the button becomes
**Save & Restart**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nicegui import run, ui
from services.settings_service import ValueSource

from nicegui_app.pages import settings_readiness
from nicegui_app.pages.settings_widgets import (
    RenderMode,
    render_mode,
    secret_placeholder,
    validate,
)
from nicegui_app.services_access import get_settings_service
from shared.config import flag_registry as registry
from shared.config.flag_registry import FlagType

_ROLE_MODEL_KEYS = (
    "MODEL_THINKING",
    "MODEL_FAST",
    "MODEL_LITE",
    "FALLBACK_MODEL",
)

_OPENROUTER_ONLY_KEYS = {
    "OPENROUTER_MODEL",
    "OPENROUTER_PROVIDER_ORDER",
    "OPENROUTER_ALLOW_FALLBACKS",
    "OPENROUTER_REQUIRE_PARAMETERS",
}

_OPENROUTER_ROUTE_FALLBACKS = {
    "google": {
        "google-vertex": "Google Vertex",
        "google-ai-studio": "Google AI Studio",
    },
    "openai": {"openai": "OpenAI"},
    "anthropic": {"anthropic": "Anthropic"},
    "amazon": {"amazon-bedrock": "Amazon Bedrock"},
}


@dataclass(frozen=True)
class ModelSectionPlan:
    primary_keys: list[str]
    remaining_keys: list[str]
    show_openrouter_routes: bool


def _coerce_for_save(flag, value: Any) -> Any:
    if flag.type is FlagType.BOOL:
        return bool(value)
    if flag.type is FlagType.INT:
        return int(value) if value not in (None, "") else 0
    if flag.type is FlagType.FLOAT:
        return float(value) if value not in (None, "") else 0.0
    return "" if value is None else str(value)


def _model_select_options(svc, current: dict[str, Any]) -> dict[str, Any]:
    """Build provider/model select options for the active provider."""
    provider = _selected_provider(current)
    gemini_models = svc.get_gemini_models()
    openrouter_models = svc.get_openrouter_models() if provider == "openrouter" else []
    active_models = openrouter_models if provider == "openrouter" else gemini_models
    route_model = _to_openrouter_model(
        str(current.get("MODEL_FAST") or (active_models[0] if active_models else ""))
    )
    provider_routes = (
        svc.get_openrouter_provider_routes(route_model) if provider == "openrouter" else {}
    )
    options: dict[str, Any] = {
        "__GEMINI_MODELS": gemini_models,
        "__OPENROUTER_MODELS": openrouter_models,
        "LLM_PROVIDER": svc.get_llm_provider_options(),
        "MODEL_THINKING": active_models,
        "MODEL_FAST": active_models,
        "MODEL_LITE": active_models,
        "FALLBACK_MODEL": active_models,
        "OPENROUTER_PROVIDER_ORDER": provider_routes,
    }
    # Any registry flag opted into a Gemini-only model picker (as opposed to
    # the provider-aware role model fields above) gets its options built here
    # generically, so new flags need no edit to this page.
    for flag in registry.FLAGS.values():
        if flag.model_picker == "gemini":
            options[flag.name] = gemini_models
    return options


def _selected_provider(values: dict[str, Any]) -> str:
    provider = str(values.get("LLM_PROVIDER") or "gemini").strip().lower()
    return "openrouter" if provider in {"openrouter", "open-router"} else "gemini"


def _to_openrouter_model(model: str) -> str:
    model = str(model or "").strip()
    if not model:
        return model
    if "/" in model:
        return model
    if model.startswith("gemini-"):
        return f"google/{model}"
    return model


def _to_gemini_model(model: str) -> str:
    model = str(model or "").strip()
    if model.startswith("google/gemini-"):
        return model.split("/", 1)[1]
    return model


def _model_for_provider(model: Any, provider: str) -> str:
    raw = str(model or "").strip()
    if provider == "openrouter":
        return _to_openrouter_model(raw)
    return _to_gemini_model(raw)


def _apply_llm_provider_change(pending: dict[str, Any], provider: str) -> set[str]:
    provider = _selected_provider({"LLM_PROVIDER": provider})
    changed: set[str] = set()
    if pending.get("LLM_PROVIDER") != provider:
        pending["LLM_PROVIDER"] = provider
        changed.add("LLM_PROVIDER")
    for key in _ROLE_MODEL_KEYS:
        if key not in pending:
            continue
        old = pending.get(key)
        new = _model_for_provider(old, provider)
        if new != old:
            pending[key] = new
            changed.add(key)
    return changed


def _model_section_plan(names: list[str], pending: dict[str, Any]) -> ModelSectionPlan:
    provider = _selected_provider(pending)
    hidden = {"OPENROUTER_MODEL"}
    if provider != "openrouter":
        hidden.update(_OPENROUTER_ONLY_KEYS)
    primary_order = [
        "LLM_PROVIDER",
        *_ROLE_MODEL_KEYS,
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    ]
    primary = [key for key in primary_order if key in names and key not in hidden]
    remaining = [key for key in names if key not in primary and key not in hidden]
    return ModelSectionPlan(
        primary_keys=primary,
        remaining_keys=remaining,
        show_openrouter_routes=provider == "openrouter",
    )


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


def _role_model_options(model_options: dict[str, Any], pending: dict[str, Any]) -> list[str]:
    if _selected_provider(pending) == "openrouter":
        return list(
            model_options.get("__OPENROUTER_MODELS")
            or model_options.get("MODEL_FAST")
            or []
        )
    return list(
        model_options.get("__GEMINI_MODELS")
        or model_options.get("MODEL_FAST")
        or []
    )


def _route_fallbacks_for_model(model: Any) -> dict[str, str]:
    provider_prefix = str(model or "").split("/", 1)[0].lower()
    return dict(_OPENROUTER_ROUTE_FALLBACKS.get(provider_prefix, {}))


def _matches(flag, query: str) -> bool:
    if not query:
        return True
    needle = query.strip().lower()
    return (
        needle in flag.name.lower()
        or needle in flag.display_label.lower()
        or needle in flag.description.lower()
    )


def visible_flags(
    group_id: str,
    pending: dict[str, Any],
    show_advanced: bool,
    query: str,
) -> list:
    """Flags to render in ``group_id`` under the current filters.

    A search query overrides the advanced filter -- if you searched for it by
    name you want to see it, wherever it sits in the tiering.
    """
    out = []
    for flag in registry.flags_in_group(group_id):
        if not flag.show_in_settings:
            continue
        if flag.depends_on and not pending.get(flag.depends_on):
            continue
        if flag.advanced and not show_advanced and not query:
            continue
        if not _matches(flag, query):
            continue
        out.append(flag)
    return out


def group_is_inert(group_id: str, pending: dict[str, Any]) -> bool:
    """True when the group is essentially *about* one switch that is off.

    Showing Grafana's dashboard/panel pickers when the Grafana server is
    disabled, or the Layout Engine's knobs when grid design is off, is the
    single largest source of noise on this page. But a group can also host a
    lone flag that depends on some unrelated toggle defined elsewhere without
    the group itself being about that toggle -- that single flag being off
    must not hide LLM_PROVIDER and the rest of an otherwise unconditional
    group. Require most of the group's flags to share the dependency before
    treating the whole section as inert.
    """
    flags = [f for f in registry.flags_in_group(group_id) if f.show_in_settings]
    if not flags:
        return False
    dependencies = {f.depends_on for f in flags if f.depends_on}
    if len(dependencies) != 1:
        return False
    dependency = dependencies.pop()
    if any(f.name == dependency for f in flags):
        return False  # the master switch lives in this group; keep it reachable
    gated = sum(1 for f in flags if f.depends_on == dependency)
    if gated * 2 <= len(flags):
        return False  # only a minority of flags depend on it; not really about it
    return not bool(pending.get(dependency))


async def render(log_levels: list[str] | None = None) -> None:
    ui.label("⚙️ Bot Settings").classes("text-h5")
    ui.label("Configure Anansi bot behavior and features.").classes("text-caption")

    svc = get_settings_service()
    current: dict[str, Any] = await run.io_bound(
        lambda: svc.get_current_settings(fetch_from_do=True)
    )
    provenance: dict[str, Any] = await run.io_bound(
        lambda: svc.get_settings_with_provenance(fetch_from_do=True)
    )
    secret_state: dict[str, bool] = {
        name: entry.secret_is_set for name, entry in provenance.items()
    }
    log_levels = log_levels or svc.get_log_levels()

    pending: dict[str, Any] = dict(current)
    model_options: dict[str, Any] = await run.io_bound(lambda: _model_select_options(svc, current))
    models_container: Any = None

    # Grafana dashboard/panel catalogue (from Supabase) powers the chip pickers.
    # Loaded once; falls back to empty (raw text fields) if the DB is unreachable.
    grafana_dashboards: dict[str, str] = {}
    grafana_panels: dict[str, dict] = {}
    if not group_is_inert("grafana", pending):
        try:
            from services.grafana_metadata_service import (
                load_available_dashboards,
                load_panels_metadata,
            )

            grafana_dashboards = await run.io_bound(load_available_dashboards) or {}
            grafana_panels = await run.io_bound(load_panels_metadata) or {}
        except Exception:  # noqa: BLE001 — degrade to raw-field editing, never break the page
            grafana_dashboards, grafana_panels = {}, {}

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
            if flag.secret and not str(val or "").strip():
                continue  # blank secret field means "unchanged", never "clear it"
            if _coerce_for_save(flag, val) != _coerce_for_save(flag, current.get(name)):
                out[name] = _coerce_for_save(flag, val)
        return out

    def _refresh_bar() -> None:
        save_bar.clear()
        changed = _changed()
        needs_restart = any(registry.FLAGS[k].restart_required for k in changed)
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
        for name, val in changed.items():
            error = validate(registry.FLAGS[name], val)
            if error:
                ui.notify(error + " — not saved.", type="negative")
                return
        ok, err = await run.io_bound(lambda: svc.update_settings(changed, restart_bot=restart))
        if ok:
            current.update(changed)
            for name in changed:
                if registry.FLAGS[name].secret:
                    secret_state[name] = True
                    pending[name] = ""
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

    def _rerender_models_section() -> None:
        if models_container is None:
            return
        models_container.clear()
        with models_container:
            names = [
                f.name
                for f in visible_flags("models", pending, state["advanced"], state["query"])
            ]
            _render_models_section(
                names, pending, log_levels, model_options, _on_change,
                secret_state, provenance, set(_changed()),
            )

    def _on_change(name: str, value: Any) -> None:
        rerender_models = False
        if name == "LLM_PROVIDER":
            _apply_llm_provider_change(pending, value)
            rerender_models = True
        else:
            pending[name] = value
            rerender_models = name in _ROLE_MODEL_KEYS
        if rerender_models:
            _rerender_models_section()
        _refresh_bar()

    def _render_group_body(group_id: str, names: list) -> None:
        nonlocal models_container
        if group_id == "grafana":
            _render_grafana_section(
                [f.name for f in names],
                pending,
                log_levels,
                model_options,
                _on_change,
                grafana_dashboards,
                grafana_panels,
                secret_state,
                provenance,
                set(_changed()),
            )
        elif group_id == "models":
            models_container = ui.column().classes("w-full")
            with models_container:
                _render_models_section(
                    [f.name for f in names], pending, log_levels, model_options, _on_change,
                    secret_state, provenance, set(_changed()),
                )
        else:
            # Two-column grid: most flags are short (toggle/number/one-line
            # string) and read better side-by-side. JSON textareas opt out
            # of the column span (see _render_flag) since they're long.
            with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-0"):
                for flag in names:
                    _render_flag(
                        flag.name, pending, log_levels, model_options, _on_change,
                        secret_state, provenance, set(_changed()),
                    )

    state = {"query": "", "advanced": False}
    groups_container: Any = None
    readiness_container: Any = None

    def _rebuild_groups() -> None:
        groups_container.clear()
        with groups_container:
            for group in registry.groups():
                flags = visible_flags(group.id, pending, state["advanced"], state["query"])
                if not flags:
                    continue
                inert = group_is_inert(group.id, pending)
                changed_here = sum(1 for f in flags if f.name in _changed())
                header = group.title
                if changed_here:
                    header += f"  ·  {changed_here} changed"
                expanded = bool(state["query"]) or group.id in ("bot_control", "models")
                section = ui.expansion(header, value=expanded and not inert).classes(
                    "w-full q-mb-sm"
                )
                section.props(
                    'header-class="text-h6 text-weight-bold" expand-icon-class="text-h5" '
                    "dense-toggle switch-toggle-side"
                )
                with section:
                    ui.label(group.description).classes("text-caption").style(
                        "color: #64748b"
                    )
                    if inert:
                        ui.label(
                            "Disabled — turn the corresponding server on in "
                            "Tools & Integrations to configure this."
                        ).classes("text-caption text-warning")
                        continue
                    _render_group_body(group.id, flags)

    def _rebuild_readiness() -> None:
        readiness_container.clear()
        with readiness_container:
            settings_readiness.render_panel(settings_readiness.build_rows())

    readiness_container = ui.column().classes("w-full")
    _rebuild_readiness()

    with ui.row().classes("items-center gap-3 w-full q-mb-sm").style(
        "position: sticky; top: 0; z-index: 10; background: #f0f2f6; padding: 0.5rem 0"
    ):

        def _on_search(event) -> None:
            state["query"] = event.value or ""
            _rebuild_groups()

        def _on_advanced(event) -> None:
            state["advanced"] = bool(event.value)
            _rebuild_groups()

        ui.input(placeholder="Search settings…", on_change=_on_search).props(
            "outlined dense clearable"
        ).classes("flex-grow")
        ui.switch("Show advanced", value=False, on_change=_on_advanced).props("dense")

    groups_container = ui.column().classes("w-full")
    _rebuild_groups()

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
    secret_state: dict[str, bool] | None = None,
    provenance: dict[str, Any] | None = None,
    changed_names: set[str] | None = None,
) -> None:
    plan = _model_section_plan(names, pending)

    provider_routes = model_options.get("OPENROUTER_PROVIDER_ORDER") or {}
    selected_model = pending.get("MODEL_FAST") or ""
    if plan.show_openrouter_routes and not provider_routes:
        provider_routes = _route_fallbacks_for_model(selected_model)
    section_model_options = dict(model_options)
    section_model_options["OPENROUTER_PROVIDER_ORDER"] = provider_routes
    if plan.show_openrouter_routes:
        with ui.card().classes("w-full q-mb-md").style("grid-column: 1 / -1"):
            ui.label("OpenRouter provider route").classes("text-subtitle1 text-weight-bold")
            if provider_routes:
                ui.label(
                    "Provider endpoint routes available for the selected main model. "
                    "If that provider has BYOK configured with “Always use for this provider,” "
                    "OpenRouter will use it for the route."
                ).classes("text-caption").style("color: #64748b")
                for provider, label in provider_routes.items():
                    ui.label(f"{provider} · {label}").classes("text-caption")
            else:
                ui.label(
                    "No provider routes were discovered for the selected main model. "
                    "The picker still accepts the current value if one is configured."
                ).classes("text-caption").style("color: #64748b")
            if selected_model:
                ui.label(f"Routes shown for: {selected_model}").classes("text-caption").style(
                    "color: #64748b"
                )

    with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-0"):
        for key in plan.primary_keys:
            _render_flag(
                key, pending, log_levels, section_model_options, on_change,
                secret_state, provenance, changed_names,
            )
        for name in plan.remaining_keys:
            _render_flag(
                name, pending, log_levels, section_model_options, on_change,
                secret_state, provenance, changed_names,
            )


def _render_grafana_section(
    names: list[str],
    pending: dict,
    log_levels: list[str],
    model_options: dict[str, Any],
    on_change,
    available_dashboards: dict[str, str],
    panels_metadata: dict[str, dict],
    secret_state: dict[str, bool] | None = None,
    provenance: dict[str, Any] | None = None,
    changed_names: set[str] | None = None,
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
    ):
        if _take(key):
            _render_flag(
                key, pending, log_levels, model_options, on_change,
                secret_state, provenance, changed_names,
            )

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
                    _render_flag(
                        key, pending, log_levels, model_options, on_change,
                        secret_state, provenance, changed_names,
                    )
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

    # Any remaining Grafana flags (GRAFANA_PASSWORD, future additions) render
    # generically -- secret_state/provenance/changed_names must still flow
    # through here or GRAFANA_PASSWORD's masked placeholder loses its real
    # configured/not-configured status.
    for name in remaining:
        _render_flag(
            name, pending, log_levels, model_options, on_change,
            secret_state, provenance, changed_names,
        )


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


_PROVENANCE_CHIP = {
    ValueSource.DEFAULT: ("default", "#94a3b8"),
    ValueSource.ENVIRONMENT: ("set in environment", "#22c55e"),
    ValueSource.BACKEND: ("set in deployment", "#22c55e"),
}


def _render_flag(
    name: str,
    pending: dict,
    log_levels: list[str],
    model_options: dict[str, Any],
    on_change,
    secret_state: dict[str, bool] | None = None,
    provenance: dict[str, Any] | None = None,
    changed_names: set[str] | None = None,
) -> None:
    flag = registry.FLAGS[name]
    value = pending.get(name)
    mode = render_mode(flag)
    label = flag.display_label

    def handler(e, n=name) -> None:
        on_change(n, e.value)

    wrapper = ui.column().classes("gap-0 w-full q-mb-sm")
    # CSS grid items default to min-width: auto, so a value with no natural break
    # point (a token, a JWT, DigitalOcean's "EV[1:...]" encrypted placeholder)
    # forces the track wider than the column instead of wrapping, overlapping
    # the next column. Letting the item shrink is what makes the label's own
    # overflow-wrap rule actually take effect.
    wrapper.style("min-width: 0")
    if mode is RenderMode.TEXTAREA:
        # JSON/long-prompt blobs read poorly squeezed into a half-width grid
        # cell — span both columns. A no-op outside a grid container (e.g. the
        # Grafana section's own layout).
        wrapper.style("grid-column: 1 / -1")
    with wrapper:
        if mode is RenderMode.READ_ONLY or mode is RenderMode.READ_ONLY_SECRET:
            if mode is RenderMode.READ_ONLY_SECRET:
                display_value = secret_placeholder(value not in (None, ""))
            else:
                display_value = value if value not in (None, "") else "—"
            ui.label(f"{label}: {display_value}").classes("text-body2").style(
                "overflow-wrap: anywhere; word-break: break-word"
            )
            if flag.set_via:
                ui.label(flag.set_via).classes("text-caption").style("color: #64748b")
            if flag.description:
                ui.label(flag.description).classes("text-caption").style("color: #64748b")
            return

        if mode is RenderMode.SECRET:
            is_set = bool((secret_state or {}).get(name))
            ui.input(
                label,
                value="",
                password=True,
                placeholder=secret_placeholder(is_set),
                on_change=handler,
            ).classes("w-full")
            ui.label("Leave blank to keep the current value.").classes("text-caption").style(
                "color: #64748b"
            )
        elif mode is RenderMode.SWITCH:
            ui.switch(label, value=bool(value), on_change=handler)
        elif mode is RenderMode.SELECT and name == "LOG_LEVEL":
            opts = log_levels or list(flag.choices)
            ui.select(opts, value=value, label=label, on_change=handler).classes("w-full")
        elif mode is RenderMode.SELECT:
            ui.select(
                list(flag.choices), value=value, label=label, on_change=handler
            ).classes("w-full")
        elif mode is RenderMode.NUMBER:
            number_args: dict[str, Any] = {}
            if flag.minimum is not None:
                number_args["min"] = flag.minimum
            if flag.maximum is not None:
                number_args["max"] = flag.maximum
            if flag.type is FlagType.INT:
                number_args["precision"] = 0
            ui.number(label, value=value, on_change=handler, **number_args).classes("w-full")
        elif mode is RenderMode.MULTI_SELECT and name == "OPENROUTER_PROVIDER_ORDER":
            opts = _options_with_current(model_options.get(name, {}), value)
            current = _csv_to_list(value)
            if isinstance(opts, dict):
                for provider in current:
                    opts.setdefault(provider, provider)
            else:
                opts = _options_with_current(opts, value)
            (
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
        elif mode is RenderMode.MULTI_SELECT:
            entries = _csv_to_list(value)
            (
                ui.select(
                    {entry: entry for entry in entries},
                    value=entries,
                    label=label,
                    multiple=True,
                    with_input=True,
                    new_value_mode="add-unique",
                    on_change=lambda e, n=name: on_change(n, ",".join(e.value or [])),
                )
                .props("use-chips outlined dense clearable")
                .classes("w-full")
            )
        elif mode is RenderMode.TEXTAREA:
            ui.textarea(label, value=str(value or ""), on_change=handler).classes("w-full")
        elif name in _ROLE_MODEL_KEYS:
            opts = _options_with_current(_role_model_options(model_options, pending), value)
            (
                ui.select(opts, value=value, label=label, with_input=True, on_change=handler)
                .props("outlined dense clearable")
                .classes("w-full")
            )
        elif name in model_options:
            opts = _options_with_current(model_options[name], value)
            (
                ui.select(opts, value=value, label=label, with_input=True, on_change=handler)
                .props("outlined dense clearable")
                .classes("w-full")
            )
        else:
            ui.input(label, value=str(value or ""), on_change=handler).classes("w-full")

        if flag.description:
            ui.label(flag.description).classes("text-caption").style("color: #64748b")

        source = (provenance or {}).get(name)
        source = source.source if source is not None else None
        if changed_names and name in changed_names:
            chip_text, chip_color = "changed here", "#f59e0b"
        elif source in _PROVENANCE_CHIP:
            chip_text, chip_color = _PROVENANCE_CHIP[source]
        else:
            chip_text, chip_color = None, None
        if chip_text:
            ui.label(chip_text).classes("text-caption").style(
                f"color: {chip_color}; font-size: 0.7rem; letter-spacing: 0.03em;"
            )
