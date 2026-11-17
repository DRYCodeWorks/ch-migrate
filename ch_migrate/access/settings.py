"""What `config.yaml` says about one environment, without resolving any secret.

`ch_migrate.config.get_env_config` reads every password as it loads, from SSM or the
environment. `access validate` must run with neither, so this loader reads the same keys,
merged the same way, and keeps SSM paths as paths. `get_env_config` keeps its shape,
because client code imports it.

The live commands, `access show` and `access plan`, read the server's address from here too,
and then resolve only the admin's secret (`server.py`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ch_migrate.access.reading import yaml_problem
from ch_migrate.bootstrap import validate_identifier
from ch_migrate.config import load_config

# How long a live read waits for a replica that is behind before a row counts as absent (2.5).
READ_TIMEOUT = 30.0


class SettingsError(ValueError):
    """config.yaml is missing, or doesn't describe the environment well enough."""


@dataclass(frozen=True)
class ServerSettings:
    """Where the environment's server is; the live `access` commands connect to it."""

    host: str | None = None
    port: int | None = None
    secure: bool = True
    cluster: str | None = None
    aws_region: str | None = None
    # `access_read_timeout`: seconds to keep re-reading before a missing row counts as absent.
    read_timeout: float = READ_TIMEOUT


@dataclass(frozen=True)
class ProjectSettings:
    """One environment's names from config.yaml. Secrets stay where they are."""

    environment: str
    database: str
    project: str
    migration_user: str
    admin_user: str = "default"
    mcp_user_name: str | None = None
    dict_reader_name: str | None = None
    # SSM paths by secret name, as written; nothing here has been read.
    ssm: dict[str, Any] = field(default_factory=dict)
    environments: tuple[str, ...] = ()
    server: ServerSettings = ServerSettings()
    # config.yaml's `migration_role`: a role another project directory shares (2.8).
    shared_role: str | None = None

    def placeholders(self) -> dict[str, str]:
        """Values for `{db}`, `{project}`, `{env}` and `{migration_role}` (2.3)."""
        return {
            "db": self.database,
            "project": self.project,
            "env": self.environment,
            "migration_role": self.migration_role,
        }

    @property
    def migration_role(self) -> str:
        """config.yaml's `migration_role`, else the role 0.8's `bootstrap` names after the
        project."""
        return self.shared_role or f"{self.project}_migration_role"


def load_settings(environment: str, config_path: Path) -> ProjectSettings:
    """Read one environment from config.yaml, as `get_env_config` would, minus the secrets."""
    config = _read_config(config_path)
    environments = _mapping(config, "environments", "environments")
    if environment not in environments:
        available = ", ".join(environments) or "(none)"
        raise SettingsError(f"Unknown environment: {environment}. Available: {available}")
    own = _mapping(environments, environment, f"environments.{environment}")
    merged = {**_mapping(config, "defaults", "defaults"), **own}
    database = _required(merged, ("database",), environment)
    settings = ProjectSettings(
        environment=environment,
        database=database,
        project=_project_name(config, merged, database),
        migration_user=_required(merged, ("migration_user", "user"), environment),
        admin_user=merged.get("admin_user") or "default",
        mcp_user_name=merged.get("mcp_user_name"),
        dict_reader_name=merged.get("dict_reader_name"),
        ssm=_mapping(own, "ssm", f"environments.{environment}.ssm"),
        environments=tuple(environments),
        server=_server(merged),
        shared_role=str(merged["migration_role"]) if merged.get("migration_role") else None,
    )
    _check_names(settings)
    return settings


def _read_config(path: Path) -> dict[str, Any]:
    try:
        config = load_config(path)
    except FileNotFoundError as error:
        raise SettingsError(str(error)) from None
    except yaml.YAMLError as error:
        raise SettingsError(": ".join(part for part in (path.name, *yaml_problem(error)) if part))
    if not isinstance(config, dict):
        raise SettingsError(f"{path.name} must be a mapping")
    return config


def _mapping(parent: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    """`parent[key]` as a mapping; unset reads as empty."""
    value = parent.get(key) or {}
    if not isinstance(value, dict):
        raise SettingsError(f"{label} in config.yaml must be a mapping")
    return dict(value)


def _required(merged: dict[str, Any], keys: tuple[str, ...], environment: str) -> str:
    """The first of `keys` that is set; `migration_user` falls back to 0.8's `user`."""
    value = next((merged[key] for key in keys if merged.get(key)), None)
    if not value:
        raise SettingsError(f"{keys[0]} is required in config.yaml for environment '{environment}'")
    return str(value)


def _project_name(config: dict[str, Any], merged: dict[str, Any], database: str) -> str:
    """`project.name`, else a `project` key in the environment, else the database name up
    to its first underscore: the order 0.8's `bootstrap` uses."""
    project = config.get("project")
    if isinstance(project, dict) and project.get("name"):
        return str(project["name"])
    return str(merged.get("project") or database.split("_")[0])


def _server(merged: dict[str, Any]) -> ServerSettings:
    """The connection keys, read as `get_env_config` and `connection.get_client` read them."""
    return ServerSettings(
        host=merged.get("host"),
        port=merged.get("port"),
        secure=bool(merged.get("secure", True)),
        cluster=_cluster(merged.get("cluster")),
        aws_region=merged.get("aws_region"),
        read_timeout=_read_timeout(merged.get("access_read_timeout", READ_TIMEOUT)),
    )


def _cluster(value: Any) -> str | None:
    """The cluster's name, when set. Its characters aren't checked: the SQL quotes it."""
    if value is not None and not isinstance(value, str):
        raise SettingsError("cluster in config.yaml must be a string")
    return value


def _read_timeout(value: Any) -> float:
    # The bound refuses .inf and .nan, and an integer too large to be seconds as a float.
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not number or not 0 <= value <= sys.float_info.max:
        raise SettingsError(
            "access_read_timeout in config.yaml must be a number of seconds, 0 or more"
        )
    return float(value)


def _check_names(settings: ProjectSettings) -> None:
    labels = {
        "database name": settings.database,
        "project name": settings.project,
        "migration user": settings.migration_user,
        "migration role": settings.shared_role,
        "MCP user": settings.mcp_user_name,
        "dict reader user": settings.dict_reader_name,
    }
    for label, name in labels.items():
        if name is not None:
            try:
                validate_identifier(name, label)
            except ValueError as error:
                raise SettingsError(str(error)) from None
