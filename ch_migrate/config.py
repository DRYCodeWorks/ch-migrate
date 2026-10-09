"""Configuration loading for ch-migrate-cli."""

from pathlib import Path
from typing import Any

import yaml

from ch_migrate.secrets import get_secret


def load_config(config_path: Path) -> dict[str, Any]:
    """
    Load YAML configuration file.

    Args:
        config_path: Path to config.yaml

    Returns:
        Parsed configuration dictionary

    Raises:
        FileNotFoundError: If config file doesn't exist
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path) as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


def get_env_config(env_name: str, config_path: Path) -> dict[str, Any]:
    """
    Get configuration for a specific environment, merging defaults.

    Loads non-secret config from YAML, secrets from environment variables.
    Supports both new field names (migration_user, migration_password) and
    legacy names (user, password) for backward compatibility.

    Args:
        env_name: Environment name (dev, staging, production, etc.)
        config_path: Path to config.yaml

    Returns:
        Complete environment configuration with secrets

    Raises:
        ValueError: If environment not found or required secrets missing
    """
    config = load_config(config_path)

    environments = config.get("environments", {})
    if env_name not in environments:
        available = ", ".join(environments.keys()) or "(none)"
        raise ValueError(f"Unknown environment: {env_name}. Available: {available}")

    # Merge defaults with environment-specific config
    defaults = config.get("defaults", {})
    env_config = {**defaults, **environments[env_name]}

    # Add project name from config if available
    project_config = config.get("project", {})
    if isinstance(project_config, dict) and "name" in project_config:
        env_config["project"] = project_config["name"]

    # Get SSM config if present (for secret lookups)
    ssm_config = environments[env_name].get("ssm", {})
    if ssm_config:
        env_config["ssm"] = ssm_config

    # Get AWS region for SSM lookups (optional, uses AWS default if not set)
    aws_region = env_config.get("aws_region")

    # Load secrets using unified get_secret() - uses SSM if configured, otherwise env vars
    # Migration password is required
    password = get_secret(
        env_name,
        "migration_password",
        ssm_path=ssm_config.get("migration_password"),
        aws_region=aws_region,
        required=True,
    )
    env_config["password"] = password  # Legacy field for backward compat

    # Optional: admin password (for bootstrap)
    env_config["admin_password"] = get_secret(
        env_name,
        "admin_password",
        ssm_path=ssm_config.get("admin_password"),
        aws_region=aws_region,
        required=False,
    )

    # Optional: dict_reader password (for dictionaries)
    env_config["dict_reader_password"] = get_secret(
        env_name,
        "dict_reader_password",
        ssm_path=ssm_config.get("dict_reader_password"),
        aws_region=aws_region,
        required=False,
    )

    # Optional: MCP user password (for read-only access)
    env_config["mcp_password"] = get_secret(
        env_name,
        "mcp_password",
        ssm_path=ssm_config.get("mcp_password"),
        aws_region=aws_region,
        required=False,
    )

    # Pass through top-level hooks section (used by env.py for pre/post migrate)
    hooks_config = config.get("hooks")
    if hooks_config:
        env_config["hooks"] = hooks_config

    return env_config
