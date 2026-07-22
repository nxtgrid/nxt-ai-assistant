"""The tool manifest must stay in sync across its three layers.

Each MCP server declares its tools three times:

1. ``tool_definitions.json`` — what the orchestrator actually serves in
   production (``server_registry.list_tools`` prefers it when present).
2. The code manifest — ``handle_list_tools`` / the ``tool_schemas`` modules,
   which are also what ``scripts/export_tools.py`` regenerates the JSON from.
3. The ``handle_call_tool`` dispatch — what is actually implemented.

These drifted badly before: 32 tools (17 customer, 7 knowledge, 5 schedule,
2 jira, 1 messaging) lived only in the JSON, so running export_tools.py would
have silently deleted them from production. These tests pin the invariants
that make the export safe:

- every JSON tool exists, under the same name, in the code manifest
  (export can only add tools, never drop them);
- every advertised tool (JSON or code) has a dispatch branch, so nothing is
  offered to the LLM that would fail with "Unknown tool";
- servers with runtime-computed manifests (grafana builds its tool list from
  dashboard metadata in the DB) must never be frozen into the JSON, because
  the JSON entry would permanently override the live list.

Extraction is static (AST) so the tests run without the servers' runtime
dependencies. Tool names must therefore be string literals — which they are,
and should stay: a dynamically-computed tool name in a schema or dispatch
belongs in DYNAMIC_MANIFEST_SERVERS.
"""

import ast
import json
import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[1]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from server_registry import SERVER_METADATA  # noqa: E402

# Servers whose tool list is computed at runtime and cannot be statically
# pinned. Mirrors scripts/export_tools.py DYNAMIC_MANIFEST_SERVERS.
DYNAMIC_MANIFEST_SERVERS = {"grafana"}

# Dispatch functions that handle_call_tool may delegate to per server.
_DISPATCH_FUNCS = ("handle_call_tool", "_handle_internal_tool")


def _server_file(server_name: str) -> Path:
    module_path = SERVER_METADATA[server_name]["module"]
    return _MCP_ROOT / (module_path.replace(".", "/") + ".py")


def _advertised_names(server_name: str) -> set:
    """Tool names in the code manifest: Tool(name=...) calls plus schema dicts
    (with both 'name' and 'inputSchema' keys) in the server module and its
    tool_schemas sibling."""
    names = set()
    server_path = _server_file(server_name)
    paths = [server_path]
    schemas_path = server_path.parent / "tool_schemas.py"
    if schemas_path.exists():
        paths.append(schemas_path)

    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if fname == "Tool":
                    for kw in node.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            names.add(kw.value.value)
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                if "name" in keys and "inputSchema" in keys:
                    for k, v in zip(node.keys, node.values):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "name"
                            and isinstance(v, ast.Constant)
                        ):
                            names.add(v.value)
    return names


def _dispatched_names(server_name: str) -> set:
    """Tool names compared against the dispatch parameter in handle_call_tool
    (and the helpers it delegates to)."""
    tree = ast.parse(_server_file(server_name).read_text())
    names = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _DISPATCH_FUNCS
            and node.args.args
        ):
            param = node.args.args[0].arg
            for sub in ast.walk(node):
                if isinstance(sub, ast.Compare) and (
                    isinstance(sub.left, ast.Name) and sub.left.id == param
                ):
                    for comp in sub.comparators:
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            names.add(comp.value)
                        elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                            for elt in comp.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    names.add(elt.value)
                if isinstance(sub, ast.Match) and (
                    isinstance(sub.subject, ast.Name) and sub.subject.id == param
                ):
                    for case in sub.cases:
                        if isinstance(case.pattern, ast.MatchValue) and isinstance(
                            case.pattern.value, ast.Constant
                        ):
                            names.add(case.pattern.value.value)
    return names


def _normalize(server_name: str, names: set) -> set:
    """Strip the server-name prefix some servers re-add in dispatch (jira
    compares against 'jira_get_issue' while advertising 'get_issue')."""
    prefix = f"{server_name}_"
    return {n[len(prefix) :] if n.startswith(prefix) else n for n in names}


def _json_manifest() -> dict:
    data = json.loads((_MCP_ROOT / "tool_definitions.json").read_text())
    return data["tools"]


def test_json_manifest_is_subset_of_code_manifest():
    """Every tool prod serves from the JSON must exist under the same name in
    the code manifest, or running export_tools.py deletes it from prod."""
    problems = []
    for server_name, tools in _json_manifest().items():
        assert server_name in SERVER_METADATA, f"JSON lists unknown server: {server_name}"
        advertised = _advertised_names(server_name)
        missing = sorted({t["name"] for t in tools} - advertised)
        if missing:
            problems.append(f"{server_name}: {missing}")
    assert not problems, (
        "tool_definitions.json advertises tools absent from the code manifest "
        "(handle_list_tools / tool_schemas) — export_tools.py would delete them:\n  "
        + "\n  ".join(problems)
    )


def test_every_json_tool_is_dispatchable():
    """A tool advertised in the JSON with no dispatch branch fails at call time."""
    problems = []
    for server_name, tools in _json_manifest().items():
        dispatched = _normalize(server_name, _dispatched_names(server_name))
        missing = sorted(_normalize(server_name, {t["name"] for t in tools}) - dispatched)
        if missing:
            problems.append(f"{server_name}: {missing}")
    assert not problems, (
        "tool_definitions.json advertises tools with no handle_call_tool branch:\n  "
        + "\n  ".join(problems)
    )


def test_every_advertised_tool_is_dispatchable():
    """Same guarantee for the code manifest itself, across all servers."""
    problems = []
    for server_name in SERVER_METADATA:
        if server_name in DYNAMIC_MANIFEST_SERVERS:
            continue
        advertised = _normalize(server_name, _advertised_names(server_name))
        dispatched = _normalize(server_name, _dispatched_names(server_name))
        missing = sorted(advertised - dispatched)
        if missing:
            problems.append(f"{server_name}: {missing}")
    assert not problems, (
        "code manifest advertises tools with no handle_call_tool branch:\n  "
        + "\n  ".join(problems)
    )


def test_dynamic_manifest_servers_not_frozen_in_json():
    """server_registry prefers the JSON entry, so freezing a runtime-computed
    manifest (grafana's DB-driven panel list) would override the live list."""
    frozen = DYNAMIC_MANIFEST_SERVERS & set(_json_manifest())
    assert not frozen, (
        f"servers with runtime-computed manifests frozen into tool_definitions.json: "
        f"{sorted(frozen)}. Remove them from the JSON and from export_tools.py's export."
    )


def test_every_registered_server_advertises_something():
    """A server whose static manifest is empty is either dead or dynamic —
    dynamic ones belong in DYNAMIC_MANIFEST_SERVERS."""
    json_manifest = _json_manifest()
    empty = [
        s
        for s in SERVER_METADATA
        if s not in DYNAMIC_MANIFEST_SERVERS
        and not _advertised_names(s)
        and not json_manifest.get(s)
    ]
    assert not empty, f"servers advertising no tools at all: {empty}"
