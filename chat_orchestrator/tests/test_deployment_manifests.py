"""Tests for deployment manifests that must preserve runtime bot settings."""

from pathlib import Path


def test_deployment_manifests_declare_telegram_bot_username():
    """Manifest-based deploys preserve the group-chat mention setting."""
    repo_root = Path(__file__).resolve().parents[2]

    for path in (repo_root / "chat_orchestrator/project.yml", repo_root / ".do/app.example.yaml"):
        assert "TELEGRAM_BOT_USERNAME" in path.read_text()
