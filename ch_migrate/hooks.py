"""Pre/post migration hook support for ch-migrate-cli."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)


@dataclass
class HookRegistry:
    """Registry of pre/post migration hooks loaded from config.yaml.

    Config format:
        hooks:
          pre_migrate:
            - "SELECT 1"
          post_migrate:
            - "SYSTEM RELOAD DICTIONARY {db}.dict_regions ON CLUSTER default"
    """

    pre_migrate: list[str] = field(default_factory=list)
    post_migrate: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, hooks_config: dict[str, Any] | None) -> HookRegistry:
        """Build a HookRegistry from the hooks section of config.yaml."""
        if not hooks_config:
            return cls()

        pre = hooks_config.get("pre_migrate", [])
        post = hooks_config.get("post_migrate", [])

        if not isinstance(pre, list):
            pre = [pre] if pre else []
        if not isinstance(post, list):
            post = [post] if post else []

        return cls(pre_migrate=pre, post_migrate=post)

    @property
    def has_hooks(self) -> bool:
        return bool(self.pre_migrate or self.post_migrate)


def run_hooks(
    connection: Connection,
    hooks: list[str],
    *,
    db: str,
    phase: str,
    revision: str,
) -> None:
    """Execute a list of hook SQL statements.

    Args:
        connection: SQLAlchemy connection to ClickHouse
        hooks: List of SQL strings (may contain {db} placeholder)
        db: Database name for placeholder resolution
        phase: "pre_migrate" or "post_migrate" (for logging)
        revision: Migration revision being processed (for logging)
    """
    for i, hook_sql in enumerate(hooks, 1):
        # Use explicit replace instead of str.format() to avoid KeyError on
        # unknown placeholders (e.g. {cluster}) and ClickHouse parameterized
        # query syntax like {param:String}.
        resolved = hook_sql.replace("{db}", db)
        logger.info("[%s] hook %d/%d for %s: %s", phase, i, len(hooks), revision, resolved)
        connection.execute(text(resolved))
        connection.commit()
