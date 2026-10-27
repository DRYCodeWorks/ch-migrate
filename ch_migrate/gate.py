"""Static safety checks for the pending upgrade set, before Alembic executes."""

from pathlib import Path

from dotenv import load_dotenv

from ch_migrate import ui
from ch_migrate.config import get_env_config, load_config
from ch_migrate.connection import get_migration_state
from ch_migrate.display import lint_finding_line
from ch_migrate.lint import GATE_RULES, LintConfig, LintReport, Severity, lint_migrations
from ch_migrate.rebase import build_revision_graph
from ch_migrate.statements import pending_revisions


def enforce_up_gate(project_root: Path, environment: str, skip_mv_check: bool = False) -> None:
    """Print the pending upgrades' findings, and refuse the run if a gate rule failed."""
    try:
        report = lint_pending_up(project_root, environment, skip_mv_check)
    except Exception as error:
        raise ui.Failure(f"Migration preflight failed: {error}") from error
    if print_gate_findings(report):
        raise ui.Failure(
            "Migration gate failed; nothing was applied.",
            "Fix SQL or add a reasoned in-file waiver.",
        )


def print_gate_findings(report: LintReport) -> bool:
    blocked = False
    for finding in report.results:
        gate_error = finding.rule in GATE_RULES and finding.severity == Severity.ERROR
        blocked |= gate_error
        severity = finding.severity.value
        if severity == "error" and not gate_error:
            severity = "warn"
        position = finding.file or "config.yaml"
        if finding.line is not None:
            position += f":{finding.line}"
        console = ui.err if gate_error else ui.out
        console.print(lint_finding_line(finding, location=position, severity=severity))
        if finding.statement:
            ui.detail(finding.statement, stderr=gate_error)
    return blocked


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
    pending = pending_revisions(graph, get_migration_state(env_config).heads)
    return lint_migrations(versions, config=config, revisions=pending)
