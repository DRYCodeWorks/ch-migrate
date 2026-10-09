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
    LargeTableMutationRule,
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
    def test_defaults(self):
        config = LintConfig()
        assert config.large_table_threshold == 100_000_000
        assert config.rules == {}

    def test_from_config_empty(self):
        config = LintConfig.from_config({})
        assert config.large_table_threshold == 100_000_000

    def test_from_config_with_values(self):
        config = LintConfig.from_config({
            "lint": {
                "large_table_threshold": 50_000,
                "rules": {
                    "destructive_changes": "error",
                    "missing_on_cluster": "off",
                },
            }
        })
        assert config.large_table_threshold == 50_000
        assert config.rules["destructive_changes"] == Severity.ERROR
        assert config.rules["missing_on_cluster"] == Severity.OFF

    def test_from_config_invalid_severity_ignored(self):
        config = LintConfig.from_config({
            "lint": {
                "rules": {"destructive_changes": "invalid_value"},
            }
        })
        assert "destructive_changes" not in config.rules


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
        report = LintReport(results=[
            LintResult(rule="test", message="bad", severity=Severity.ERROR),
            LintResult(rule="test", message="meh", severity=Severity.WARN),
        ])
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
        sql = textwrap.dedent("""\
            DROP TABLE mydb.old_events;
            ALTER TABLE mydb.users DROP COLUMN phone;
        """)
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
        sql = textwrap.dedent("""\
            CREATE TABLE mydb.t (
                `id` UInt64,
                `key` String,
                `select` String
            )
        """)
        results = ReservedWordRule().check(sql)
        reserved_names = {r.message.split("'")[1] for r in results}
        assert "key" in reserved_names
        assert "select" in reserved_names

    def test_passes_non_reserved_names(self):
        sql = textwrap.dedent("""\
            CREATE TABLE mydb.t (
                `user_id` UInt64,
                `event_name` String
            )
        """)
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
        sql = textwrap.dedent("""\
            ALTER TABLE mydb.t ADD COLUMN foo String;
            DROP TABLE mydb.t;
        """)
        results = MissingOnClusterRule().check(sql, config=config)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# LargeTableMutationRule tests
# ---------------------------------------------------------------------------


class TestLargeTableMutationRule:
    def _make_client(self, row_count: int) -> MagicMock:
        client = MagicMock()
        result = MagicMock()
        result.result_rows = [[row_count]]
        client.query.return_value = result
        return client

    def test_flags_large_table(self):
        client = self._make_client(200_000_000)
        sql = "ALTER TABLE mydb.users ADD COLUMN phone String"
        results = LargeTableMutationRule().check(
            sql, client=client, database="mydb"
        )
        assert len(results) == 1
        assert "200,000,000" in results[0].message

    def test_passes_small_table(self):
        client = self._make_client(1000)
        sql = "ALTER TABLE mydb.users ADD COLUMN phone String"
        results = LargeTableMutationRule().check(
            sql, client=client, database="mydb"
        )
        assert results == []

    def test_respects_custom_threshold(self):
        client = self._make_client(5000)
        config = LintConfig(large_table_threshold=1000)
        sql = "ALTER TABLE mydb.users ADD COLUMN phone String"
        results = LargeTableMutationRule().check(
            sql, client=client, database="mydb", config=config
        )
        assert len(results) == 1

    def test_skips_without_client(self):
        sql = "ALTER TABLE mydb.users ADD COLUMN phone String"
        results = LargeTableMutationRule().check(sql)
        assert results == []


# ---------------------------------------------------------------------------
# MVDependencyRule tests
# ---------------------------------------------------------------------------


class TestMVDependencyRule:
    def _make_client_with_deps(self) -> MagicMock:
        """Create a mock client that returns a dependency graph with MV on events."""
        from ch_migrate.introspect import (
            DepType,
            DependencyEdge,
            DependencyGraph,
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
            results = MVDependencyRule().check(
                sql, client=client, database="mydb"
            )

        assert len(results) == 1
        assert "hourly_mv" in results[0].message

    def test_passes_on_unrelated_table(self):
        from unittest.mock import patch

        client, graph = self._make_client_with_deps()
        sql = "DROP TABLE IF EXISTS unrelated_table"

        with patch("ch_migrate.introspect.get_dependencies", return_value=graph):
            results = MVDependencyRule().check(
                sql, client=client, database="mydb"
            )

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
        content = textwrap.dedent(f"""\
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
        """)

        file_path = versions_dir / f"{rev_id}_{name}.py"
        file_path.write_text(content)
        return versions_dir

    def test_static_lint_finds_issues(self, tmp_path: Path):
        versions_dir = self._create_migration(
            tmp_path, "drop_users", "DROP TABLE mydb.users"
        )
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
        versions_dir = self._create_migration(
            tmp_path, "drop_bad", "DROP TABLE mydb.users"
        )
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
