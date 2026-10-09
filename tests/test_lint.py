"""Tests for migration linting rules."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ch_migrate.lint import (
    ALL_RULES,
    RUNTIME_RULES,
    STATIC_RULES,
    DestructiveChangeRule,
    IdempotencyRule,
    LintConfig,
    LintReport,
    LintResult,
    MissingOnClusterRule,
    MVDependencyRule,
    ReservedWordRule,
    Severity,
    lint_migrations,
)

# ---------------------------------------------------------------------------
# LintConfig tests
# ---------------------------------------------------------------------------


class TestLintConfig:
    def test_from_config_with_values(self):
        config = LintConfig.from_config(
            {
                "lint": {
                    "rules": {
                        "destructive_changes": "error",
                        "missing_on_cluster": "off",
                    },
                }
            }
        )
        assert config.rules["destructive_changes"] == Severity.ERROR
        assert config.rules["missing_on_cluster"] == Severity.OFF

    def test_from_config_invalid_severity_ignored(self):
        config = LintConfig.from_config(
            {
                "lint": {
                    "rules": {"destructive_changes": "invalid_value"},
                }
            }
        )
        assert "destructive_changes" not in config.rules

    def test_retired_size_config_is_ignored_with_one_warning(self, capsys):
        config = LintConfig.from_config(
            {
                "lint": {
                    "large_table_threshold": 1,
                    "rules": {"large_table_mutation": "error", "destructive_changes": "warn"},
                }
            }
        )
        assert config.rules == {"destructive_changes": Severity.WARN}
        warning = capsys.readouterr().err
        assert len(warning.splitlines()) == 1
        assert "ch-migrate plan" in warning


# ---------------------------------------------------------------------------
# LintReport tests
# ---------------------------------------------------------------------------


class TestLintReport:
    def test_empty_report(self):
        report = LintReport()
        assert not report.has_errors
        assert report.error_count == 0
        assert report.warning_count == 0

    def test_report_with_errors(self):
        report = LintReport(
            results=[
                LintResult(rule="test", message="bad", severity=Severity.ERROR),
                LintResult(rule="test", message="meh", severity=Severity.WARN),
            ]
        )
        assert report.has_errors
        assert report.error_count == 1
        assert report.warning_count == 1


# ---------------------------------------------------------------------------
# DestructiveChangeRule tests
# ---------------------------------------------------------------------------


class TestDestructiveChangeRule:
    def test_flags_drop_table(self):
        sql = "DROP TABLE mydb.users"
        results = DestructiveChangeRule().check(sql)
        assert len(results) == 1
        assert "DROP TABLE" in results[0].message
        assert results[0].severity == Severity.WARN

    def test_flags_drop_column(self):
        sql = "ALTER TABLE mydb.users DROP COLUMN email"
        results = DestructiveChangeRule().check(sql)
        assert len(results) == 1
        assert "DROP COLUMN" in results[0].message

    def test_no_flags_on_safe_sql(self):
        sql = "CREATE TABLE mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = DestructiveChangeRule().check(sql)
        assert results == []

    def test_multiple_drops(self):
        sql = textwrap.dedent(
            """\
            DROP TABLE mydb.old_events;
            ALTER TABLE mydb.users DROP COLUMN phone;
        """
        )
        results = DestructiveChangeRule().check(sql)
        assert len(results) == 2

    def test_respects_severity_off(self):
        config = LintConfig(rules={"destructive_changes": Severity.OFF})
        results = DestructiveChangeRule().check("DROP TABLE foo", config=config)
        assert results == []

    def test_respects_severity_error(self):
        config = LintConfig(rules={"destructive_changes": Severity.ERROR})
        results = DestructiveChangeRule().check("DROP TABLE foo", config=config)
        assert len(results) == 1
        assert results[0].severity == Severity.ERROR

    def test_reports_line_number(self):
        sql = "SELECT 1;\nSELECT 2;\nDROP TABLE foo;"
        results = DestructiveChangeRule().check(sql)
        assert results[0].line == 3


# ---------------------------------------------------------------------------
# IdempotencyRule tests
# ---------------------------------------------------------------------------


class TestIdempotencyRule:
    def test_flags_create_without_if_not_exists(self):
        sql = "CREATE TABLE mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = IdempotencyRule().check(sql)
        assert len(results) == 1
        assert "IF NOT EXISTS" in results[0].message

    def test_passes_with_if_not_exists(self):
        sql = "CREATE TABLE IF NOT EXISTS mydb.users (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = IdempotencyRule().check(sql)
        assert results == []

    def test_passes_with_or_replace(self):
        sql = "CREATE OR REPLACE DICTIONARY mydb.dict_foo (key String) PRIMARY KEY key"
        results = IdempotencyRule().check(sql)
        assert results == []

    def test_flags_drop_without_if_exists(self):
        sql = "DROP TABLE mydb.users"
        results = IdempotencyRule().check(sql)
        assert len(results) == 1
        assert "IF EXISTS" in results[0].message

    def test_passes_drop_with_if_exists(self):
        sql = "DROP TABLE IF EXISTS mydb.users"
        results = IdempotencyRule().check(sql)
        assert results == []

    def test_flags_create_view_without_if_not_exists(self):
        sql = "CREATE VIEW mydb.v AS SELECT 1"
        results = IdempotencyRule().check(sql)
        assert len(results) == 1

    def test_flags_create_materialized_view(self):
        sql = "CREATE MATERIALIZED VIEW mydb.mv TO mydb.dest AS SELECT 1 FROM mydb.src"
        results = IdempotencyRule().check(sql)
        assert len(results) == 1

    def test_passes_create_mv_if_not_exists(self):
        sql = "CREATE MATERIALIZED VIEW IF NOT EXISTS mydb.mv TO mydb.dest AS SELECT 1"
        results = IdempotencyRule().check(sql)
        assert results == []


# ---------------------------------------------------------------------------
# ReservedWordRule tests
# ---------------------------------------------------------------------------


class TestReservedWordRule:
    def test_flags_reserved_column_name(self):
        sql = textwrap.dedent(
            """\
            CREATE TABLE mydb.t (
                `id` UInt64,
                `key` String,
                `select` String
            )
        """
        )
        results = ReservedWordRule().check(sql)
        reserved_names = {r.message.split("'")[1] for r in results}
        assert "key" in reserved_names
        assert "select" in reserved_names

    def test_passes_non_reserved_names(self):
        sql = textwrap.dedent(
            """\
            CREATE TABLE mydb.t (
                `user_id` UInt64,
                `event_name` String
            )
        """
        )
        results = ReservedWordRule().check(sql)
        assert results == []

    def test_case_insensitive(self):
        sql = "    `KEY` String"
        results = ReservedWordRule().check(sql)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# MissingOnClusterRule tests
# ---------------------------------------------------------------------------


class TestMissingOnClusterRule:
    def test_off_by_default(self):
        sql = "CREATE TABLE mydb.t (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = MissingOnClusterRule().check(sql)
        assert results == []

    def test_flags_when_enabled(self):
        config = LintConfig(rules={"missing_on_cluster": Severity.WARN})
        sql = "CREATE TABLE mydb.t (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = MissingOnClusterRule().check(sql, config=config)
        assert len(results) == 1
        assert "ON CLUSTER" in results[0].message

    def test_passes_with_on_cluster(self):
        config = LintConfig(rules={"missing_on_cluster": Severity.WARN})
        sql = "CREATE TABLE mydb.t ON CLUSTER default (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = MissingOnClusterRule().check(sql, config=config)
        assert results == []

    def test_passes_with_placeholder(self):
        config = LintConfig(rules={"missing_on_cluster": Severity.WARN})
        sql = "CREATE TABLE mydb.t {on_cluster} (id UInt64) ENGINE = MergeTree ORDER BY id"
        results = MissingOnClusterRule().check(sql, config=config)
        assert results == []

    def test_flags_alter_and_drop(self):
        config = LintConfig(rules={"missing_on_cluster": Severity.WARN})
        sql = textwrap.dedent(
            """\
            ALTER TABLE mydb.t ADD COLUMN foo String;
            DROP TABLE mydb.t;
        """
        )
        results = MissingOnClusterRule().check(sql, config=config)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# MVDependencyRule tests
# ---------------------------------------------------------------------------


class TestMVDependencyRule:
    def _make_client_with_deps(self) -> MagicMock:
        """Create a mock client that returns a dependency graph with MV on events."""
        from ch_migrate.introspect import (
            DependencyEdge,
            DependencyGraph,
            DepType,
            ObjectNode,
        )

        graph = DependencyGraph()
        graph.nodes = {
            "events": ObjectNode(name="events", obj_type="table"),
            "hourly_mv": ObjectNode(name="hourly_mv", obj_type="materialized_view"),
        }
        graph.edges = [
            DependencyEdge(source="events", target="hourly_mv", dep_type=DepType.SCHEMA),
        ]

        client = MagicMock()
        # Patch get_dependencies to return our mock graph
        return client, graph

    def test_flags_drop_on_mv_source(self):
        from unittest.mock import patch

        client, graph = self._make_client_with_deps()
        sql = "DROP TABLE IF EXISTS events"

        with patch("ch_migrate.introspect.get_dependencies", return_value=graph):
            results = MVDependencyRule().check(sql, client=client, database="mydb")

        assert len(results) == 1
        assert "hourly_mv" in results[0].message

    def test_passes_on_unrelated_table(self):
        from unittest.mock import patch

        client, graph = self._make_client_with_deps()
        sql = "DROP TABLE IF EXISTS unrelated_table"

        with patch("ch_migrate.introspect.get_dependencies", return_value=graph):
            results = MVDependencyRule().check(sql, client=client, database="mydb")

        assert results == []

    def test_skips_without_client(self):
        sql = "DROP TABLE IF EXISTS events"
        results = MVDependencyRule().check(sql)
        assert results == []


# ---------------------------------------------------------------------------
# Rule registry tests
# ---------------------------------------------------------------------------


class TestRuleRegistry:
    def test_static_rules_dont_require_db(self):
        for rule in STATIC_RULES:
            assert not rule.requires_db, f"{rule.name} should not require DB"

    def test_runtime_rules_require_db(self):
        for rule in RUNTIME_RULES:
            assert rule.requires_db, f"{rule.name} should require DB"

    def test_all_rules_have_names(self):
        for rule in ALL_RULES:
            assert rule.name, f"Rule {type(rule).__name__} missing name"

    def test_all_rule_names_unique(self):
        names = [r.name for r in ALL_RULES]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Integration: lint_migrations with temp files
# ---------------------------------------------------------------------------


class TestLintMigrations:
    def _create_migration(self, tmp_path: Path, name: str, sql_content: str) -> Path:
        """Create a migration file with embedded SQL in op.execute()."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir(exist_ok=True)

        rev_id = name[:12].ljust(12, "0")
        content = textwrap.dedent(
            f"""\
            \"\"\"Migration {name}

            Revision ID: {rev_id}
            Revises:
            Create Date: 2024-01-01

            \"\"\"
            from alembic import op

            revision = '{rev_id}'
            down_revision = None

            def upgrade():
                op.execute(\"\"\"{sql_content}\"\"\")

            def downgrade():
                pass
        """
        )

        file_path = versions_dir / f"{rev_id}_{name}.py"
        file_path.write_text(content)
        return versions_dir

    def test_static_lint_finds_issues(self, tmp_path: Path):
        versions_dir = self._create_migration(tmp_path, "drop_users", "DROP TABLE mydb.users")
        report = lint_migrations(versions_dir)
        assert report.warning_count > 0

    def test_static_lint_clean(self, tmp_path: Path):
        versions_dir = self._create_migration(
            tmp_path,
            "safe_migration",
            "CREATE TABLE IF NOT EXISTS mydb.t (id UInt64) ENGINE = MergeTree ORDER BY id",
        )
        report = lint_migrations(versions_dir)
        assert report.error_count == 0
        assert report.warning_count == 0

    def test_lint_with_error_severity(self, tmp_path: Path):
        versions_dir = self._create_migration(tmp_path, "drop_bad", "DROP TABLE mydb.users")
        config = LintConfig(rules={"destructive_changes": Severity.ERROR})
        report = lint_migrations(versions_dir, config=config)
        assert report.has_errors
        assert report.error_count >= 1

    def test_lint_empty_versions_dir(self, tmp_path: Path):
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()
        report = lint_migrations(versions_dir)
        assert not report.has_errors
        assert report.results == []


class TestStandaloneSetRule:
    def test_set_legacy_env_is_an_error(self, tmp_path):
        legacy = (Path(__file__).parent / "fixtures/env_v0_4_1.py").read_text()
        versions = self._project(tmp_path, "SET max_threads = 3", legacy)
        [finding] = lint_migrations(versions).results
        assert finding.rule == "standalone_set"
        assert finding.severity == Severity.ERROR
        assert finding.file == "migrations/versions/set.py" and finding.line == 4
        assert "upgrade-env" in finding.message and "SETTINGS clause" in finding.message

    def test_set_v2_env_is_silent_without_importing_it(self, tmp_path):
        versions = self._project(
            tmp_path,
            "SET max_threads = 3",
            "CH_MIGRATE_ENV_VERSION = 2\nraise RuntimeError('must not import')\n",
        )
        assert lint_migrations(versions).results == []

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 SETTINGS max_threads = 3",
            "SELECT 'SET max_threads = 3; still a string'",
            "SELECT 'SETTINGS max_threads = 3'",
        ],
    )
    def test_set_ignores_settings_clauses_and_strings(self, tmp_path, sql):
        versions = self._project(tmp_path, sql, None)
        assert lint_migrations(versions).results == []

    def test_set_missing_marker_and_mixed_case(self, tmp_path):
        versions = self._project(tmp_path, "-- note\nsEt max_threads = 3", "# custom env\n")
        [finding] = lint_migrations(versions).results
        assert finding.rule == "standalone_set" and finding.severity == Severity.ERROR

    def _project(self, root, sql, environment):
        versions = root / "migrations/versions"
        versions.mkdir(parents=True)
        if environment is not None:
            (versions.parent / "env.py").write_text(environment)
        (versions / "set.py").write_text(
            "revision = 'set'\ndown_revision = None\ndef upgrade():\n" f"    op.execute({sql!r})\n"
        )
        return versions
