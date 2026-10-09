"""Gate refusals precede side effects; repaired migrations reach the clean schema."""

import secrets

import pytest
import yaml

pytestmark = pytest.mark.integration


def test_gate_refuses_then_accepts_visible_reasoned_waiver(project):
    path = project.sql_dir / "create.sql"
    path.write_text("CREATE TABLE {db}.guarded (id UInt64) ENGINE = Memory;\n")
    _revision(project, "aaaa", "create.sql")
    for options in ((), ("--skip-mv-check",)):
        refused = project.run("up", "it", *options)
        assert refused.exit_code == 1, refused.output
        assert "migrations/sql/create.sql:1" in refused.output
        assert "IF NOT EXISTS" in refused.output
        assert "CREATE TABLE {db}.guarded" in refused.output
        assert project.client.command(f"EXISTS TABLE {project.database}.guarded") == 0
        assert project.client.command(f"EXISTS TABLE {project.database}.alembic_version") == 0
    reason = "This reviewed fixture creates a unique empty table exactly once"
    path.write_text(f"-- ch-migrate: allow-non-idempotent {reason}\n" + path.read_text())
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    assert reason in applied.output
    assert project.client.command(f"EXISTS TABLE {project.database}.guarded") == 1
    lint = project.run("lint")
    assert lint.exit_code == 0, lint.output
    assert "✓" in lint.output and "[idempotency]" in lint.output
    assert reason in " ".join(lint.output.split()) and "1 info line" in lint.output


def test_recovery_reaches_same_schema_as_clean_run(project, request):
    fixed = (
        "CREATE TABLE IF NOT EXISTS {db}.a (id UInt64) ENGINE = Memory;\n"
        "CREATE TABLE IF NOT EXISTS {db}.b (id UInt64) ENGINE = Memory;\n"
        "CREATE TABLE IF NOT EXISTS {db}.c (id UInt64) ENGINE = Memory;\n"
    )
    path = project.sql_dir / "recover.sql"
    path.write_text(fixed.replace(".b (id UInt64)", ".b (id UInt64"))
    _revision(project, "aaaa", "recover.sql")
    failed = project.run("up", "it")
    assert failed.exit_code != 0
    assert "SYNTAX_ERROR" in failed.output, failed.output
    assert project.client.command(f"EXISTS TABLE {project.database}.a") == 1
    assert project.client.command(f"EXISTS TABLE {project.database}.b") == 0
    assert project.client.command(f"EXISTS TABLE {project.database}.c") == 0
    assert (
        project.client.query(
            f"SELECT version_num FROM {project.database}.alembic_version"
        ).result_rows
        == []
    )
    path.write_text(fixed)
    repaired = project.run("up", "it")
    assert repaired.exit_code == 0, repaired.output
    recovered = _schema(project, project.database)
    clean_db = "it_" + secrets.token_hex(8)
    request.addfinalizer(lambda: project.client.command(f"DROP DATABASE IF EXISTS {clean_db} SYNC"))
    project.client.command(f"CREATE DATABASE {clean_db}")
    config_path = project.root / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["environments"]["it"]["database"] = clean_db
    config_path.write_text(yaml.safe_dump(config))
    clean = project.run("up", "it")
    assert clean.exit_code == 0, clean.output
    assert _schema(project, clean_db) == recovered


def test_baseline_exempts_old_pending_revision_but_not_new(project):
    (project.sql_dir / "old.sql").write_text("CREATE TABLE {db}.old (id UInt64) ENGINE = Memory;\n")
    _revision(project, "aaaa", "old.sql")
    config_path = project.root / "config.yaml"
    config_path.write_text(
        "# keep project context\n"
        + config_path.read_text().replace("environments:", "environments: # keep endpoint note")
    )
    upgraded = project.run("upgrade-env")
    assert upgraded.exit_code == 0, upgraded.output
    content = config_path.read_text()
    assert "# keep project context" in content and "# keep endpoint note" in content
    assert yaml.safe_load(content)["lint"]["gate_baseline"] == "aaaa"
    assert project.run("lint").exit_code == 0
    assert project.client.command(f"EXISTS TABLE {project.database}.old") == 0
    applied = project.run("up", "it")
    assert applied.exit_code == 0, applied.output
    (project.sql_dir / "new.sql").write_text("CREATE TABLE {db}.new (id UInt64) ENGINE = Memory;\n")
    project.write_revision(
        "bbbb",
        {"upgrade": "from ch_migrate import run_sql\nrun_sql('new.sql')"},
        down_revision="aaaa",
    )
    refused = project.run("up", "it")
    assert refused.exit_code == 1, refused.output
    assert "migrations/sql/new.sql:1" in refused.output
    assert project.client.command(f"EXISTS TABLE {project.database}.new") == 0
    assert project.client.query(
        f"SELECT version_num FROM {project.database}.alembic_version"
    ).result_rows == [("aaaa",)]


def test_gate_rejects_config_severity_bypass(project):
    (project.sql_dir / "create.sql").write_text(
        "CREATE TABLE {db}.guarded (id UInt64) ENGINE = Memory;\n"
    )
    _revision(project, "aaaa", "create.sql")
    config_path = project.root / "config.yaml"
    config_path.write_text(config_path.read_text() + "\nlint:\n  rules:\n    idempotency: off\n")
    for command in (("up", "it"), ("lint",)):
        result = project.run(*command)
        assert result.exit_code == 1, result.output
        assert "idempotency" in result.output and "gate_baseline" in result.output
        assert "in-file waiver" in result.output
    assert project.client.command(f"EXISTS TABLE {project.database}.guarded") == 0
    assert project.client.command(f"EXISTS TABLE {project.database}.alembic_version") == 0


def test_gate_other_findings_warn_without_blocking(project):
    (project.sql_dir / "mv.sql").write_text(
        "CREATE TABLE IF NOT EXISTS {db}.source (id UInt64) ENGINE = Memory;\n"
        "CREATE TABLE IF NOT EXISTS {db}.target (id UInt64) ENGINE = Memory;\n"
        "CREATE MATERIALIZED VIEW IF NOT EXISTS {db}.view TO {db}.target AS SELECT id FROM {db}.source;\n"
    )
    _revision(project, "aaaa", "mv.sql")
    result = project.run("up", "it")
    assert result.exit_code == 0, result.output
    assert "WARN [mv_declarations]" in result.output
    assert project.client.command(f"EXISTS TABLE {project.database}.view") == 1


def _revision(project, revision, path):
    project.write_revision(
        revision, {"upgrade": f"from ch_migrate import run_sql\nrun_sql({path!r})"}
    )


def _schema(project, database):
    return {
        table: project.client.query(f"SHOW CREATE TABLE {database}.{table}")
        .result_rows[0][0]
        .replace(database, "<db>")
        for table in ("a", "b", "c")
    }
