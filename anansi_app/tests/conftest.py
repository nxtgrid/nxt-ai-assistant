"""Shared pytest fixtures for anansi_app.

Fakes ``nicegui`` at the sys.modules level before any test module in this
directory is collected, so ``nicegui_app.pages.*`` modules (which do
``from nicegui import run, ui`` at import time) can be imported without a
real NiceGUI runtime. The CI ``validate`` job never installs
anansi_app/requirements.txt (see .github/workflows/ci.yml) -- only
mcp_servers/requirements.txt -- so this stub is required for these tests to
even collect there, let alone pass.

Centralized here (rather than repeated per test file) so a new test file
that imports a ``nicegui_app.pages.*`` module can't forget it and silently
break CI collection for the entire suite.
"""

import sys
from types import SimpleNamespace

sys.modules.setdefault("nicegui", SimpleNamespace(run=SimpleNamespace(), ui=SimpleNamespace()))
