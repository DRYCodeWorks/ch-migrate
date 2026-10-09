"""Tests for Rich display rendering."""

from io import StringIO
from pathlib import Path
from textwrap import dedent

from rich.console import Console
from typing import Optional, Tuple, Union

from clickhouse_alembic.display import render_history, render_status
from clickhouse_alembic.rebase import build_revision_graph

DownRevision = Optional[Union[str, Tuple[str, ...]]]


def _format_down_revision(down_revision: DownRevision) -> tuple[str, str]:
    if down_revision is None:
        return "None", ""
    if isinstance(down_revision, tuple):
        return repr(down_revision), ", ".join(down_revision)
    return f"'{down_revision}'", down_revision


def _write_migration(
    versions_dir: Path,
    revision: str,
    down_revision: DownRevision,
    name: str = "migration",
) -> Path:
    """Write a minimal migration file and return its path."""
    down_rev_str, revises_str = _format_down_revision(down_revision)
    content = dedent(
        f"""\
        \"""{name}

        Revision ID: {revision}
        Revises: {revises_str}
        Create Date: 2026-01-01 00:00
        \"""

        from alembic import op

        revision = '{revision}'
        down_revision = {down_rev_str}
        branch_labels = None
        depends_on = None


        def upgrade() -> None:
            pass


        def downgrade() -> None:
            pass
    """
    )
    path = versions_dir / f"2026_01_01_0000_{revision}_{name}.py"
    path.write_text(content)
    return path


def _capture_console() -> Console:
    """Create a Console that captures output to a StringIO."""
    return Console(file=StringIO(), no_color=True, width=100)


def _get_output(console: Console) -> str:
    """Get captured output from a Console."""
    console.file.seek(0)
    return console.file.read()


class TestRenderHistory:
    def test_linear_chain_all_applied(self, tmp_path):
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")
        _write_migration(tmp_path, "ccc333", "bbb222", "add_views")

        graph = build_revision_graph(tmp_path)
        applied = {"aaa111", "bbb222", "ccc333"}
        console = _capture_console()

        render_history(graph, applied, console=console)
        output = _get_output(console)

        assert "Migration History" in output
        assert "aaa111" in output
        assert "bbb222" in output
        assert "ccc333" in output
        assert "(HEAD)" in output
        # All applied — green checkmarks should appear
        assert "\u2713" in output

    def test_linear_chain_with_pending(self, tmp_path):
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")
        _write_migration(tmp_path, "ccc333", "bbb222", "add_views")

        graph = build_revision_graph(tmp_path)
        applied = {"aaa111"}
        console = _capture_console()

        render_history(graph, applied, console=console)
        output = _get_output(console)

        # Applied revision gets checkmark
        assert "\u2713" in output
        # Pending revisions get open circle
        assert "\u25cb" in output

    def test_branching_history(self, tmp_path):
        _write_migration(tmp_path, "base11", None, "base")
        _write_migration(tmp_path, "br1aaa", "base11", "branch_one")
        _write_migration(tmp_path, "br2aaa", "base11", "branch_two")

        graph = build_revision_graph(tmp_path)
        applied = {"base11", "br1aaa"}
        console = _capture_console()

        render_history(graph, applied, console=console)
        output = _get_output(console)

        assert "base11" in output
        assert "br1aaa" in output
        assert "br2aaa" in output
        # Both branches should show (HEAD)
        assert output.count("(HEAD)") == 2

    def test_db_unreachable(self, tmp_path):
        _write_migration(tmp_path, "aaa111", None, "create_tables")

        graph = build_revision_graph(tmp_path)
        console = _capture_console()

        render_history(graph, None, db_error="Connection refused", console=console)
        output = _get_output(console)

        assert "Connection refused" in output
        # Dim dash markers when DB unreachable
        assert "\u2500" in output

    def test_cycle_does_not_cause_recursion(self, tmp_path):
        """A cycle in the revision graph should not cause infinite recursion."""
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")

        graph = build_revision_graph(tmp_path)
        # Inject a cycle: bbb222 -> aaa111 (aaa111 is already parent of bbb222)
        graph.children.setdefault("bbb222", []).append("aaa111")

        console = _capture_console()
        render_history(graph, {"aaa111", "bbb222"}, console=console)
        output = _get_output(console)

        # Should render without crashing; each revision appears once
        assert "aaa111" in output
        assert "bbb222" in output

    def test_newest_first_ordering(self, tmp_path):
        """Revisions should appear newest-first (head at top, root at bottom)."""
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")
        _write_migration(tmp_path, "ccc333", "bbb222", "add_views")

        graph = build_revision_graph(tmp_path)
        applied = {"aaa111", "bbb222", "ccc333"}
        console = _capture_console()

        render_history(graph, applied, console=console)
        output = _get_output(console)

        # Head (ccc333) should appear before root (aaa111)
        assert output.index("ccc333") < output.index("aaa111")

    def test_empty_graph(self, tmp_path):
        graph = build_revision_graph(tmp_path)
        console = _capture_console()

        render_history(graph, set(), console=console)
        output = _get_output(console)

        assert "No migrations found" in output


class TestRenderStatus:
    def test_full_status_at_head(self, tmp_path):
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")

        graph = build_revision_graph(tmp_path)
        applied = {"aaa111", "bbb222"}
        env_config = {"host": "localhost", "database": "testdb", "migration_user": "migrator"}
        console = _capture_console()

        render_status("dev", env_config, graph, applied, console=console)
        output = _get_output(console)

        assert "dev" in output
        assert "localhost" in output
        assert "testdb" in output
        assert "migrator" in output
        assert "Applied" in output
        assert "2" in output
        assert "Pending" in output
        assert "0" in output
        assert "At head" in output

    def test_status_with_pending(self, tmp_path):
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")
        _write_migration(tmp_path, "ccc333", "bbb222", "add_views")

        graph = build_revision_graph(tmp_path)
        applied = {"aaa111"}
        env_config = {"host": "ch.example.com", "database": "mydb", "user": "admin"}
        console = _capture_console()

        render_status("staging", env_config, graph, applied, console=console)
        output = _get_output(console)

        assert "staging" in output
        assert "Behind by 2" in output
        assert "aaa111" in output  # last applied

    def test_status_at_merge_head_counts_all_parent_chains_applied(self, tmp_path):
        _write_migration(tmp_path, "base11", None, "base")
        _write_migration(tmp_path, "left11", "base11", "left")
        _write_migration(tmp_path, "right1", "base11", "right")
        _write_migration(tmp_path, "merge1", ("left11", "right1"), "merge")

        graph = build_revision_graph(tmp_path)
        applied = set(graph.walk_to_root("merge1"))
        env_config = {"host": "ch.example.com", "database": "mydb", "user": "admin"}
        console = _capture_console()

        render_status("staging", env_config, graph, applied, console=console)
        output = _get_output(console)

        assert "Applied" in output
        assert "4" in output
        assert "Pending" in output
        assert "0" in output
        assert "At head" in output

    def test_status_db_unreachable(self, tmp_path):
        _write_migration(tmp_path, "aaa111", None, "create_tables")
        _write_migration(tmp_path, "bbb222", "aaa111", "add_indexes")

        graph = build_revision_graph(tmp_path)
        env_config = {"host": "bad-host", "database": "db", "user": "u"}
        console = _capture_console()

        render_status("dev", env_config, graph, None, db_error="timeout", console=console)
        output = _get_output(console)

        # Applied state is unknown, but the local count and head still show.
        assert "unknown" in output
        assert "2" in output
        assert "bbb222" in output

    def test_status_uses_migration_user_over_user(self, tmp_path):
        graph = build_revision_graph(tmp_path)
        env_config = {
            "host": "h",
            "database": "d",
            "migration_user": "mig_user",
            "user": "fallback",
        }
        console = _capture_console()

        render_status("dev", env_config, graph, set(), console=console)
        output = _get_output(console)

        assert "mig_user" in output
