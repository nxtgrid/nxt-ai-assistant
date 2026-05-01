"""
Shared credential retrieval utilities with function composition.

Provides composable functions for retrieving credentials from environment
with fallback defaults.
"""

import os
from typing import Callable, Dict, Tuple


def get_env_or_default(env_var: str, default: str = "") -> str:
    """
    Get environment variable or default value.

    Pure function: env_var -> value

    Args:
        env_var: Environment variable name
        default: Default value if not set

    Returns:
        Environment variable value or default
    """
    return os.getenv(env_var, default)


def compose_credential_getter(*env_vars: str) -> Callable[..., Tuple[str, ...]]:
    """
    Compose a credential getter function from environment variable names.

    Higher-order function that returns a getter.

    Args:
        *env_vars: Environment variable names in order

    Returns:
        Function that gets credentials with optional overrides
    """

    def get_credentials(**overrides) -> Tuple[str, ...]:
        """
        Get credentials from environment with optional overrides.

        Args:
            **overrides: Optional override values

        Returns:
            Tuple of credential values in same order as env_vars
        """
        return tuple(
            overrides.get(var.lower().replace("_", ""), get_env_or_default(var)) for var in env_vars
        )

    return get_credentials


def create_multi_credential_getter(
    credential_map: Dict[str, Tuple[str, ...]],
) -> Dict[str, Callable]:
    """
    Create multiple credential getters from a mapping.

    Function factory pattern.

    Args:
        credential_map: Dict of name -> (env_var1, env_var2, ...)

    Returns:
        Dict of name -> credential_getter_function
    """
    return {name: compose_credential_getter(*env_vars) for name, env_vars in credential_map.items()}


# Common credential patterns


def get_chirpstack_credentials() -> Tuple[str, str, str]:
    """
    Get Chirpstack API credentials from environment.

    Returns:
        Tuple of (base_url, api_key, tenant_id)
    """
    getter = compose_credential_getter(
        "CHIRPSTACK_BASE_URL", "CHIRPSTACK_API_KEY", "CHIRPSTACK_TENANT_ID"
    )
    result = getter()
    return (result[0], result[1], result[2])


def get_jira_credentials() -> Tuple[str, str, str]:
    """
    Get Jira API credentials from environment.

    Returns:
        Tuple of (base_url, username, api_token)
    """
    getter = compose_credential_getter("JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN")
    result = getter()
    return (result[0], result[1], result[2])


def get_vrm_credentials() -> Tuple[str, str]:
    """
    Get VRM API credentials from environment.

    Returns:
        Tuple of (token, user_id)
    """
    getter = compose_credential_getter("VRM_TOKEN", "VRM_USER_ID")
    result = getter()
    return (result[0], result[1])
