"""Static safety checks for the pending upgrade set, before Alembic executes."""

from pathlib import Path

from dotenv import load_dotenv

from ch_migrate.config import get_env_config, load_config
from ch_migrate.connection import get_current_heads
from ch_migrate.lint import LintConfig, LintReport, Severity, lint_migrations
from ch_migrate.rebase import build_revision_graph
from ch_migrate.statements import pending_revisions


def lint_pending_up(
    project_root: Path, environment: str, skip_mv_check: bool = False
) -> LintReport:
    load_dotenv(project_root / ".env.local")
    config_path = project_root / "config.yaml"
    config = LintConfig.from_config(load_config(config_path))
    if skip_mv_check:
        config.rules["mv_declarations"] = Severity.OFF
    versions = project_root / "migrations" / "versions"
    graph = build_revision_graph(versions)
    env_config = get_env_config(environment, config_path)
    pending = pending_revisions(graph, get_current_heads(env_config))
    return lint_migrations(versions, config=config, revisions=pending)
