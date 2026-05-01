"""Bot status service — polls DigitalOcean deployments API and health endpoint.

Returns a status used to style the sidebar logo with a live indicator.
Called from a st.fragment(run_every=30s) in app.py — no internal caching needed.
"""

import logging
import os
from typing import Literal

import requests

logger = logging.getLogger(__name__)

BotStatus = Literal["live", "deploying", "down"]

_DEPLOYING_PHASES = ("BUILDING", "DEPLOYING", "PENDING_BUILD", "PENDING_DEPLOY")


def get_bot_status() -> BotStatus:
    """Determine current bot status from DO deployment phase + health check.

    Priority: deploying > down > live.
    Short-circuits: skips health check if deployment phase is already conclusive.
    """
    deployment_phase = _get_deployment_phase()

    # If actively deploying, that's the status — skip health check
    if deployment_phase in _DEPLOYING_PHASES:
        return "deploying"

    # Health check determines live vs down
    if _check_health():
        return "live"
    return "down"


def _get_deployment_phase() -> str:
    """Fetch the latest deployment phase from the DigitalOcean API."""
    token = os.getenv("DIGITALOCEAN_API_TOKEN", "").strip()
    app_id = os.getenv("DIGITALOCEAN_APP_ID")
    if not token or not app_id:
        return "UNKNOWN"
    try:
        resp = requests.get(
            f"https://api.digitalocean.com/v2/apps/{app_id}/deployments",
            headers={"Authorization": f"Bearer {token}"},
            params={"per_page": 1},
            timeout=3,
        )
        if resp.status_code == 200:
            deployments = resp.json().get("deployments", [])
            if deployments:
                phase: str = deployments[0].get("phase", "UNKNOWN")
                return phase
        return "UNKNOWN"
    except Exception:
        logger.debug("DO deployment API check failed", exc_info=True)
        return "UNKNOWN"


def _check_health() -> bool:
    """Ping the anansi-bot health endpoint."""
    bot_url = os.getenv("ANANSI_BOT_HEALTH_URL", "http://localhost:8000/health")
    try:
        resp = requests.get(bot_url, timeout=3)
        return resp.status_code == 200
    except Exception:
        logger.debug("Bot health check failed", exc_info=True)
        return False
