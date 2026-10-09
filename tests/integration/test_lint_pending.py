"""Live lint excludes applied revisions and reports the pending SQL location."""

import re

import pytest

pytestmark = pytest.mark.integration


def test_lint_pending_reports_only_second_revision(project, monkeypatch):
    (project.sql_dir / "a.sql").write_text("DROP TABLE IF EXISTS {db}.applied_only;\n")
    (project.sql_dir / "b.sql").write_text(
        "-- pending change\n\nDROP TABLE IF EXISTS {db}.pending_only;\n"
    )
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('a.sql')"}
    )
    project.write_revision(
        "bbbb",
        {"upgrade": "from ch_migrate import run_sql\nrun_sql('b.sql')"},
        down_revision="aaaa",
    )
    applied = project.run("up", "it", "-r", "aaaa")
    assert applied.exit_code == 0, applied.output
    monkeypatch.setenv("COLUMNS", "200")
    result = project.run("lint", "it")
    assert result.exit_code == 0, result.output
    assert "migrations/sql/a.sql" not in result.output
    assert re.search(r"migrations/sql/b\.sql[^\n]*\b3\b", result.output), result.output
    assert "destructive_changes" in result.output


def test_lint_pending_before_first_up_treats_all_revisions_as_pending(project):
    (project.sql_dir / "first.sql").write_text("DROP TABLE IF EXISTS {db}.old;\n")
    project.write_revision(
        "aaaa", {"upgrade": "from ch_migrate import run_sql\nrun_sql('first.sql')"}
    )
    result = project.run("lint", "it")
    assert result.exit_code == 0, result.output
    assert "destructive_changes" in result.output
    assert project.client.command(f"EXISTS TABLE {project.database}.alembic_version") == 0
