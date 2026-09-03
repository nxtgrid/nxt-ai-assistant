"""Tests for deployment manifests that must preserve runtime bot settings."""

import re
from pathlib import Path

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


def test_digitalocean_manifests_match_the_three_service_runtime_topology():
    source_manifest = REPO_ROOT / ".do/app.example.yaml"
    image_manifest = REPO_ROOT / ".do/app.image.example.yaml"

    assert _service_names(source_manifest) == {"chat-orchestrator", "anansi-app", "mcp-gateway"}
    assert _service_names(image_manifest) == _service_names(source_manifest)


def test_every_ghcr_manifest_image_is_published_by_the_workflow():
    workflow = REPO_ROOT / ".github/workflows/build-images.yml"
    image_manifest = REPO_ROOT / ".do/app.image.example.yaml"

    assert _manifest_image_names(image_manifest) == _published_image_names(workflow)
