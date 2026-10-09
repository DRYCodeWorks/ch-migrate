"""Tests for dependency graph command and migration validation."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from ch_migrate.deps import MigrationWarning, validate_migration
from ch_migrate.display import render_dependency_tree
from ch_migrate.introspect import (
    DependencyEdge,
    DependencyGraph,
    DepType,
    ObjectNode,
)


def _make_graph() -> DependencyGraph:
    """Build a synthetic dependency graph for testing."""
    graph = DependencyGraph()
    graph.nodes = {
        "users": ObjectNode(name="users", obj_type="table"),
        "events": ObjectNode(name="events", obj_type="table"),
        "hourly_events": ObjectNode(name="hourly_events", obj_type="materialized_view"),
        "hourly_events_dest": ObjectNode(name="hourly_events_dest", obj_type="table"),
        "dict_users": ObjectNode(name="dict_users", obj_type="dictionary"),
        "active_users": ObjectNode(name="active_users", obj_type="view"),
    }
    graph.edges = [
        DependencyEdge(source="events", target="hourly_events", dep_type=DepType.DATA_FLOW),
        DependencyEdge(source="events", target="hourly_events", dep_type=DepType.SCHEMA),
        DependencyEdge(source="hourly_events", target="hourly_events_dest", dep_type=DepType.DATA_FLOW),
        DependencyEdge(source="users", target="dict_users", dep_type=DepType.SCHEMA),
    ]
    return graph


# ---------------------------------------------------------------------------
# validate_migration tests
# ---------------------------------------------------------------------------


class TestValidateMigration:
    def test_drop_table_with_mv_dependency(self):
        graph = _make_graph()
        sql = "DROP TABLE events"
        warnings = validate_migration(sql, graph)
        assert len(warnings) >= 1
        # Should catch both schema and data_flow
        error_msgs = [w.message for w in warnings if w.severity == "error"]
        warn_msgs = [w.message for w in warnings if w.severity == "warning"]
        assert any("hourly_events" in m for m in error_msgs)  # schema dep
        assert any("hourly_events" in m for m in warn_msgs)  # data flow dep

    def test_drop_table_with_dict_dependency(self):
        graph = _make_graph()
        sql = "DROP TABLE IF EXISTS users"
        warnings = validate_migration(sql, graph)
        assert len(warnings) >= 1
        assert any("dict_users" in w.message for w in warnings)

    def test_drop_table_no_deps(self):
        graph = _make_graph()
        sql = "DROP TABLE hourly_events_dest"
        warnings = validate_migration(sql, graph)
        assert len(warnings) == 0

    def test_drop_table_not_in_graph(self):
        graph = _make_graph()
        sql = "DROP TABLE nonexistent_table"
        warnings = validate_migration(sql, graph)
        assert len(warnings) == 0

    def test_drop_materialized_view(self):
        graph = _make_graph()
        sql = "DROP MATERIALIZED VIEW hourly_events"
        warnings = validate_migration(sql, graph)
        # hourly_events has a child (hourly_events_dest via data_flow)
        assert len(warnings) >= 1
        assert any("hourly_events_dest" in w.message for w in warnings)

    def test_drop_dictionary_no_deps(self):
        graph = _make_graph()
        sql = "DROP DICTIONARY dict_users"
        warnings = validate_migration(sql, graph)
        # dict_users has no children
        assert len(warnings) == 0

    def test_safe_migration(self):
        graph = _make_graph()
        sql = "ALTER TABLE users ADD COLUMN email String"
        warnings = validate_migration(sql, graph)
        assert len(warnings) == 0

    def test_multiple_drops(self):
        graph = _make_graph()
        sql = "DROP TABLE events;\nDROP TABLE users;"
        warnings = validate_migration(sql, graph)
        assert len(warnings) >= 2


# ---------------------------------------------------------------------------
# Tree rendering tests
# ---------------------------------------------------------------------------


class TestRenderDependencyTree:
    def test_renders_tree(self):
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=100)
        graph = _make_graph()

        render_dependency_tree(graph, console=console)

        text = output.getvalue()
        assert "Dependency Graph" in text
        assert "users" in text
        assert "events" in text
        assert "hourly_events" in text
        assert "dict_users" in text

    def test_renders_empty_graph(self):
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=100)
        graph = DependencyGraph()

        render_dependency_tree(graph, console=console)

        text = output.getvalue()
        assert "No objects found" in text

    def test_shows_edge_count(self):
        import re as _re
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=100)
        graph = _make_graph()

        render_dependency_tree(graph, console=console)

        # Strip ANSI escape codes for assertion
        text = _re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
        assert "4 edges" in text

    def test_deduplicates_multi_edge_children(self):
        """Nodes with both data_flow and schema edges should appear once, not as 'circular'."""
        from io import StringIO
        from rich.console import Console
        from ch_migrate.introspect import (
            DependencyEdge,
            DependencyGraph,
            DepType,
            ObjectNode,
        )
        from ch_migrate.display import render_dependency_tree

        graph = DependencyGraph(
            nodes={
                "logs": ObjectNode(name="logs", obj_type="table"),
                "attrs_mv": ObjectNode(name="attrs_mv", obj_type="materialized_view"),
                "attrs": ObjectNode(name="attrs", obj_type="table"),
            },
            edges=[
                DependencyEdge(source="logs", target="attrs_mv", dep_type=DepType.DATA_FLOW),
                DependencyEdge(source="logs", target="attrs_mv", dep_type=DepType.SCHEMA),
                DependencyEdge(source="attrs_mv", target="attrs", dep_type=DepType.DATA_FLOW),
            ],
        )

        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=120)
        render_dependency_tree(graph, console=console)
        output = buf.getvalue()

        assert "circular" not in output
        assert "attrs_mv" in output
        assert "attrs" in output

    def test_handles_circular_dependency(self):
        graph = DependencyGraph()
        graph.nodes = {
            "a": ObjectNode(name="a", obj_type="table"),
            "b": ObjectNode(name="b", obj_type="materialized_view"),
        }
        graph.edges = [
            DependencyEdge(source="a", target="b", dep_type=DepType.SCHEMA),
            DependencyEdge(source="b", target="a", dep_type=DepType.DATA_FLOW),
        ]

        output = StringIO()
        console = Console(file=output, force_terminal=True, width=100)
        render_dependency_tree(graph, console=console)

        text = output.getvalue()
        assert "circular" in text


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


@pytest.fixture
def deps_runner(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("""
environments:
  dev:
    host: localhost
    database: testdb
    user: test
""")
    monkeypatch.setenv("CH_DEV_PASSWORD", "pass")
    return CliRunner()


class TestDepsCommand:
    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.deps.get_dependencies")
    def test_shows_graph(self, mock_deps, mock_client, deps_runner):
        mock_client.return_value = MagicMock()
        mock_deps.return_value = _make_graph()

        from ch_migrate.cli import main
        result = deps_runner.invoke(main, ["deps", "dev"])
        assert result.exit_code == 0
        assert "Dependency Graph" in result.output

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.deps.get_dependencies")
    def test_validate_safe_migration(self, mock_deps, mock_client, deps_runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_deps.return_value = _make_graph()

        sql_file = tmp_path / "safe.sql"
        sql_file.write_text("ALTER TABLE users ADD COLUMN email String")

        from ch_migrate.cli import main
        result = deps_runner.invoke(main, ["deps", "dev", "--validate", str(sql_file)])
        assert result.exit_code == 0
        assert "passed" in result.output

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.deps.get_dependencies")
    def test_validate_breaking_migration(self, mock_deps, mock_client, deps_runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_deps.return_value = _make_graph()

        sql_file = tmp_path / "break.sql"
        sql_file.write_text("DROP TABLE events")

        from ch_migrate.cli import main
        result = deps_runner.invoke(main, ["deps", "dev", "--validate", str(sql_file)])
        assert result.exit_code == 1
