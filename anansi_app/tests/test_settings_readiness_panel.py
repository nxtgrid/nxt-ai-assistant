import sys
from types import SimpleNamespace

# settings_readiness imports `ui` from nicegui at module scope for render_panel;
# build_rows itself needs none of it. Stub it the same way test_model_settings.py
# does, so this test can exercise build_rows without the nicegui dependency.
sys.modules.setdefault("nicegui", SimpleNamespace(ui=SimpleNamespace(), run=SimpleNamespace()))

from nicegui_app.pages.settings_readiness import build_rows


def _row(env, title_fragment):
    return next(r for r in build_rows(env=env) if title_fragment in r.title)


def test_satisfied_rows_sort_below_unsatisfied():
    env = {"GRID_DESIGN_DEV_NO_AUTH": "1"}
    rows = build_rows(env=env)
    first_satisfied = next(i for i, r in enumerate(rows) if r.satisfied)
    assert all(not r.satisfied for r in rows[:first_satisfied])


def test_required_rows_sort_above_recommended():
    rows = [r for r in build_rows(env={}) if not r.satisfied]
    severities = [r.severity for r in rows]
    assert severities == sorted(severities, key=lambda s: 0 if s == "required" else 1)


def test_grafana_password_is_marked_settable_here():
    # An app-owned secret the operator can finish configuring on this page.
    assert _row({}, "Grafana").settable_here is True


def test_auth_database_is_not_settable_here():
    # Host-owned; the panel must send the operator to the deployment env.
    assert _row({}, "bot can answer").settable_here is False


def test_missing_names_are_listed_verbatim():
    row = _row({}, "Grafana")
    assert "GRAFANA_URL" in row.missing
