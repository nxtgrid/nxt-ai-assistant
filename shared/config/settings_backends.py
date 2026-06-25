"""Pluggable backends for runtime settings management.

Anansi's admin UI lets operators change feature flags at runtime. Historically
this was wired directly to the DigitalOcean App Platform API, which made the
settings UI unusable on any other host (k8s, Fly, Heroku, bare metal).

This module decouples the *what* (which flags exist — see
:mod:`shared.config.flag_registry`) from the *where* (the deployment backend that
persists them). Two backends ship today:

* :class:`DigitalOceanBackend` — reads/writes the app spec via the DO API
  (the original behaviour, now driven by the flag registry).
* :class:`EnvFileBackend` — a portable default that overlays a dotenv-style file
  on ``os.environ``; works on any host.

Select a backend with the ``SETTINGS_BACKEND`` env var (``auto`` | ``digitalocean``
| ``envfile``). ``auto`` uses DigitalOcean when ``DIGITALOCEAN_APP_ID`` and
``DIGITALOCEAN_API_TOKEN`` are present, otherwise the env-file backend.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional, Tuple

from shared.config import flag_registry as registry

# Conservative per-value size cap. DigitalOcean allows ~65KB; large machine-managed
# JSON blobs (Grafana metadata) are excluded from the UI via the registry instead.
MAX_ENV_VAR_SIZE = 32000


def _as_env_str(value: Any) -> str:
    """Render a Python value the way it should appear in an env var."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _filter_writable(settings: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Drop read-only and oversized settings. Returns (writable, skipped)."""
    read_only = registry.non_editable_settings()
    writable: Dict[str, Any] = {}
    skipped: List[str] = []
    for key, value in settings.items():
        if key in read_only:
            skipped.append(key)
            continue
        if len(_as_env_str(value)) > MAX_ENV_VAR_SIZE:
            skipped.append(key)
            continue
        writable[key] = value
    return writable, skipped


class SettingsBackend(ABC):
    """Interface for reading and persisting runtime settings."""

    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """Whether this backend is usable in the current environment."""

    @abstractmethod
    def get_all(self) -> Dict[str, str]:
        """Return raw (string) values for known settings from the backend."""

    @abstractmethod
    def update(
        self, settings: Mapping[str, Any], restart: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """Persist ``settings``. Returns ``(success, error_message)``."""


class EnvFileBackend(SettingsBackend):
    """Portable backend: a dotenv file overlaid on the process environment.

    Reads return the current env (with the settings file merged on top); writes
    persist editable settings to the file at ``SETTINGS_FILE`` (default
    ``.env.settings``). Changes take effect on the next process restart — there
    is no remote control plane to redeploy.
    """

    name = "envfile"

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("SETTINGS_FILE", ".env.settings")

    def available(self) -> bool:
        return True

    def _read_file(self) -> Dict[str, str]:
        values: Dict[str, str] = {}
        if not os.path.exists(self.path):
            return values
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        return values

    def get_all(self) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        file_values = self._read_file()
        for name in registry.FLAGS:
            if name in os.environ:
                merged[name] = os.environ[name]
            elif name in file_values:
                merged[name] = file_values[name]
        return merged

    def update(
        self, settings: Mapping[str, Any], restart: bool = True
    ) -> Tuple[bool, Optional[str]]:
        writable, _skipped = _filter_writable(settings)
        existing = self._read_file()
        existing.update({k: _as_env_str(v) for k, v in writable.items()})
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(
                    "# Anansi runtime settings (written by the admin UI).\n"
                    "# Loaded on process startup; restart the service to apply changes.\n"
                )
                for key in sorted(existing):
                    handle.write(f"{key}={existing[key]}\n")
            return True, None
        except OSError as exc:
            return False, f"Failed to write settings file {self.path}: {exc}"


class DigitalOceanBackend(SettingsBackend):
    """Reads/writes settings via the DigitalOcean App Platform API."""

    name = "digitalocean"

    def __init__(self, app_id: Optional[str] = None, api_token: Optional[str] = None):
        self.app_id = app_id if app_id is not None else os.getenv("DIGITALOCEAN_APP_ID", "")
        raw_token = api_token if api_token is not None else os.getenv("DIGITALOCEAN_API_TOKEN")
        self.api_token = raw_token.strip() if raw_token else None
        self.api_base = "https://api.digitalocean.com/v2"

    def available(self) -> bool:
        return bool(self.app_id and self.api_token)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def get_all(self) -> Dict[str, str]:
        import requests  # imported lazily so non-DO hosts need no dependency

        result: Dict[str, str] = {}
        if not self.api_token:
            return result
        service_specific = registry.service_specific_settings()
        try:
            response = requests.get(
                f"{self.api_base}/apps/{self.app_id}", headers=self._headers(), timeout=10
            )
            response.raise_for_status()
            spec = response.json().get("app", {}).get("spec", {})

            for env in spec.get("envs", []):
                key, value = env.get("key"), env.get("value")
                if key and value is not None:
                    result[key] = str(value)

            for service in spec.get("services", []):
                service_name = service.get("name")
                for env in service.get("envs", []):
                    key, value = env.get("key"), env.get("value")
                    if key and value is not None and service_specific.get(key) == service_name:
                        result[key] = str(value)
            return result
        except Exception as exc:  # network/parse errors -> empty (caller falls back to env)
            print(f"Failed to fetch envs from DigitalOcean: {exc}")
            return result

    def update(
        self, settings: Mapping[str, Any], restart: bool = True
    ) -> Tuple[bool, Optional[str]]:
        import requests

        if not self.api_token:
            return False, "No DigitalOcean API token configured"
        try:
            url = f"{self.api_base}/apps/{self.app_id}"
            response = requests.get(url, headers=self._headers(), timeout=10)
            if response.status_code == 401:
                return False, "Authentication failed - invalid API token"
            if response.status_code == 403:
                return False, "Permission denied - API token needs write access to apps"
            if response.status_code == 404:
                return False, f"App not found - token may not have access to app {self.app_id}"
            if response.status_code != 200:
                return False, f"Failed to fetch app spec: HTTP {response.status_code}"

            app_spec = response.json().get("app", {}).get("spec")
            self._apply_to_spec(app_spec, settings)

            update_response = requests.put(
                url, headers=self._headers(), json={"spec": app_spec}, timeout=30
            )
            if update_response.status_code == 401:
                return False, "Authentication failed - invalid API token"
            if update_response.status_code == 403:
                return False, "Permission denied - API token needs write access to apps"
            if update_response.status_code != 200:
                error_msg = update_response.json().get("message", "Unknown error")
                return False, f"Failed to update app: {error_msg}"
            return True, None
        except Exception as exc:
            print(f"Error updating settings: {exc}")
            return False, f"Exception: {exc}"

    def _apply_to_spec(self, app_spec: Dict[str, Any], settings: Mapping[str, Any]) -> None:
        service_specific = registry.service_specific_settings()
        writable, skipped = _filter_writable(settings)
        for key in skipped:
            print(f"Skipping {key} (read-only or exceeds {MAX_ENV_VAR_SIZE} chars)")

        global_settings: Dict[str, Any] = {}
        service_settings: Dict[str, Dict[str, Any]] = {}
        for key, value in writable.items():
            service_name = service_specific.get(key)
            if service_name:
                service_settings.setdefault(service_name, {})[key] = value
            else:
                global_settings[key] = value

        if "envs" in app_spec:
            app_spec["envs"] = self._merge_env_vars(app_spec["envs"], global_settings)
        for service in app_spec.get("services", []):
            name = service.get("name")
            if name in service_settings:
                service.setdefault("envs", [])
                service["envs"] = self._merge_env_vars(service["envs"], service_settings[name])

    @staticmethod
    def _merge_env_vars(
        existing_envs: List[Dict[str, str]], settings: Mapping[str, Any]
    ) -> List[Dict[str, str]]:
        env_map = {env["key"]: env for env in existing_envs}
        for key, value in settings.items():
            str_value = _as_env_str(value)
            if key in env_map:
                env_map[key]["value"] = str_value
            else:
                env_map[key] = {"key": key, "value": str_value, "scope": "RUN_TIME"}
        return list(env_map.values())


def get_backend(prefer_remote: bool = True) -> SettingsBackend:
    """Return the configured settings backend.

    Args:
        prefer_remote: When ``SETTINGS_BACKEND=auto``, only select the remote
            (DigitalOcean) backend if it is actually available; otherwise fall
            back to the portable env-file backend.
    """
    choice = os.getenv("SETTINGS_BACKEND", "auto").strip().lower()
    if choice in ("do", "digitalocean"):
        return DigitalOceanBackend()
    if choice in ("env", "envfile", "file"):
        return EnvFileBackend()
    # auto
    do = DigitalOceanBackend()
    if prefer_remote and do.available():
        return do
    return EnvFileBackend()


__all__ = [
    "MAX_ENV_VAR_SIZE",
    "SettingsBackend",
    "EnvFileBackend",
    "DigitalOceanBackend",
    "get_backend",
]
