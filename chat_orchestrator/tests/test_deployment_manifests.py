"""Tests for deployment manifests that must preserve runtime bot settings."""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _service_names(path: Path) -> set[str]:
    return set(re.findall(r"^  - name: ([a-z0-9-]+)$", path.read_text(), re.MULTILINE))


def _published_image_names(path: Path) -> set[str]:
    return set(
        re.findall(
            r"\$\{\{ env\.IMAGE_BASE \}\}/([a-z0-9-]+):",
            path.read_text(),
        )
    )


def _manifest_image_names(path: Path) -> set[str]:
    return set(re.findall(r"^\s+repository: \S+/([a-z0-9-]+)$", path.read_text(), re.MULTILINE))


def test_deployment_manifests_declare_telegram_bot_username():
    """Manifest-based deploys preserve the group-chat mention setting."""
    for path in (REPO_ROOT / "chat_orchestrator/project.yml", REPO_ROOT / ".do/app.example.yaml"):
        assert "TELEGRAM_BOT_USERNAME" in path.read_text()


def test_deployment_manifests_declare_public_app_url_for_chat_orchestrator():
    """The notifier needs the public UI origin to deep-link internal tickets."""
    project = (REPO_ROOT / "chat_orchestrator/project.yml").read_text()
    digitalocean = (REPO_ROOT / ".do/app.example.yaml").read_text()

    assert "key: APP_URL" in project
    assert "# Canonical public URL of the Anansi app" in digitalocean


def test_digitalocean_manifests_match_the_two_service_runtime_topology():
    """The MCP gateway is mounted inside chat-orchestrator, not deployed
    separately (see orchestrator/api/app.py's _mount_mcp_gateway). A third
    service reappearing here means someone re-split it without moving the
    mount, which would leave two copies of the MCP server code deployed.
    """
    source_manifest = REPO_ROOT / ".do/app.example.yaml"
    image_manifest = REPO_ROOT / ".do/app.image.example.yaml"

    assert _service_names(source_manifest) == {"chat-orchestrator", "anansi-app"}
    assert _service_names(image_manifest) == _service_names(source_manifest)


def test_mcp_gateway_ingress_rules_point_at_chat_orchestrator_with_the_prefix_preserved():
    """All three gateway paths must reach chat-orchestrator unstripped.

    The mount is registered AT /mcp-gateway inside the app, so a rule that
    strips the prefix (the default, and what the standalone service required)
    404s every request. This asserts the flip that had to happen when the
    gateway stopped being its own service, in both manifests.
    """
    for manifest in (REPO_ROOT / ".do/app.example.yaml", REPO_ROOT / ".do/app.image.example.yaml"):
        spec = yaml.safe_load(manifest.read_text())
        gateway_rules = [
            rule
            for rule in spec["ingress"]["rules"]
            if "mcp-gateway" in rule["match"]["path"]["prefix"]
        ]

        assert {rule["match"]["path"]["prefix"] for rule in gateway_rules} == {
            "/mcp-gateway",
            "/.well-known/oauth-authorization-server/mcp-gateway",
            "/.well-known/oauth-protected-resource/mcp-gateway",
        }, manifest
        for rule in gateway_rules:
            assert rule["component"]["name"] == "chat-orchestrator", (manifest, rule)
            assert rule["component"]["preserve_path_prefix"] is True, (manifest, rule)


def test_mcp_gateway_env_vars_live_on_chat_orchestrator_and_only_the_secret_is_secret():
    """Both keys move with the gateway. MCP_GATEWAY_BASE_URL is deliberately
    not typed SECRET — it is a public URL that the discovery documents publish
    verbatim, and marking it secret only makes it harder to read back.
    """
    for manifest in (REPO_ROOT / ".do/app.example.yaml", REPO_ROOT / ".do/app.image.example.yaml"):
        spec = yaml.safe_load(manifest.read_text())
        orchestrator = next(s for s in spec["services"] if s["name"] == "chat-orchestrator")
        envs = {env["key"]: env for env in orchestrator["envs"]}

        assert envs["MCP_GATEWAY_TOKEN_SECRET"]["type"] == "SECRET", manifest
        assert "type" not in envs["MCP_GATEWAY_BASE_URL"], manifest
        assert envs["MCP_GATEWAY_BASE_URL"]["value"].endswith("/mcp-gateway"), manifest


def test_every_ghcr_manifest_image_is_published_by_the_workflow():
    workflow = REPO_ROOT / ".github/workflows/build-images.yml"
    image_manifest = REPO_ROOT / ".do/app.image.example.yaml"

    assert _manifest_image_names(image_manifest) == _published_image_names(workflow)
