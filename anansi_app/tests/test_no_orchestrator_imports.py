"""anansi_app may not import the orchestrator: its image does not ship one.

anansi_app/Dockerfile copies exactly `anansi_app/` and `shared/`, so any
`import orchestrator...` here type-checks, passes locally (where
chat_orchestrator is editable-installed) and raises ModuleNotFoundError in
production.

That is not hypothetical. The Context page's Edit dialog resolved its preview
pane through `orchestrator.services.jit_context_resolver.build_default_registry`,
inside the coroutine that builds the dialog -- so for every built-in and
document-backed module the import blew up before `dialog.open()`, NiceGUI
swallowed the handler exception, and the Edit button did nothing at all. It
was invisible in tests because the import sat inside a function no test
called, and invisible locally because `orchestrator` imports fine here.

A static scan catches the whole class rather than one instance: importing
every page module would need a NiceGUI slot context and live credentials.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent

# scripts/ are run by hand on a developer machine or in a one-off job, not
# from the deployed image, so they are not bound by what the image ships.
EXCLUDED_DIRS = {"tests", "scripts", ".venv", "__pycache__"}


def _app_sources() -> list[pathlib.Path]:
    return [
        path
        for path in APP_ROOT.rglob("*.py")
        if not EXCLUDED_DIRS & set(path.relative_to(APP_ROOT).parts)
    ]


def _orchestrator_imports(path: pathlib.Path) -> list[str]:
    """Every `orchestrator...` import in one file, including function-local ones."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "orchestrator"]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "orchestrator":
                found.append(node.module)
    return found


def test_the_scan_actually_sees_the_context_page():
    """Guards the guard: a glob that matches nothing would pass vacuously."""
    names = {p.name for p in _app_sources()}
    assert "knowledge_modules.py" in names
    assert "prompts.py" in names


@pytest.mark.parametrize("path", _app_sources(), ids=lambda p: p.name)
def test_no_module_imports_the_orchestrator(path: pathlib.Path):
    offenders = _orchestrator_imports(path)
    assert not offenders, (
        f"{path.relative_to(APP_ROOT)} imports {offenders} — `orchestrator` is not in "
        "anansi_app's image (see anansi_app/Dockerfile). Move what you need into "
        "shared/, as shared.prompts.providers.build_default_registry was."
    )
