"""Tests for deployment manifests that must preserve runtime bot settings."""

from pathlib import Path


def test_deployment_manifests_declare_telegram_bot_username():
    """Manifest-based deploys preserve the group-chat mention setting."""
    repo_root = Path(__file__).resolve().parents[2]

    for path in (repo_root / "chat_orchestrator/project.yml", repo_root / ".do/app.example.yaml"):
        assert "TELEGRAM_BOT_USERNAME" in path.read_text()


def test_deployment_manifests_declare_public_app_url_for_chat_orchestrator():
    """The notifier needs the public UI origin to deep-link internal tickets."""
    repo_root = Path(__file__).resolve().parents[2]
    project = (repo_root / "chat_orchestrator/project.yml").read_text()
    digitalocean = (repo_root / ".do/app.example.yaml").read_text()

    assert "key: APP_URL" in project
    assert "# Canonical public URL of the Anansi app" in digitalocean
