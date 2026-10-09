"""Tests for ch-migrate snapshot command."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ch_migrate.cli import main
from ch_migrate.introspect import (
    DictDefinition,
    MVDefinition,
    Schema,
    TableDefinition,
    ViewDefinition,
)


def _make_schema() -> Schema:
    """Build a synthetic Schema for testing."""
    schema = Schema(database="testdb", ch_version="24.3.1")
    schema.tables["users"] = TableDefinition(
        name="users",
        engine="MergeTree",
        raw_ddl="CREATE TABLE testdb.users (id UInt64) ENGINE = MergeTree ORDER BY id",
    )
    schema.tables["events"] = TableDefinition(
        name="events",
        engine="MergeTree",
        raw_ddl="CREATE TABLE testdb.events (id UInt64) ENGINE = MergeTree ORDER BY id",
    )
    schema.tables["peerdb_staging"] = TableDefinition(
        name="peerdb_staging",
        engine="MergeTree",
        raw_ddl="CREATE TABLE testdb.peerdb_staging (id UInt64) ENGINE = MergeTree ORDER BY id",
    )
    schema.views["active_users"] = ViewDefinition(
        name="active_users",
        select_query="SELECT * FROM users WHERE active = 1",
        raw_ddl="CREATE VIEW testdb.active_users AS SELECT * FROM users WHERE active = 1",
    )
    schema.materialized_views["hourly_events"] = MVDefinition(
        name="hourly_events",
        raw_ddl="CREATE MATERIALIZED VIEW testdb.hourly_events TO testdb.hourly_dest AS SELECT count() FROM events",
    )
    schema.dictionaries["dict_topic"] = DictDefinition(
        name="dict_topic",
        raw_ddl="CREATE DICTIONARY testdb.dict_topic (key String) PRIMARY KEY key SOURCE(CLICKHOUSE(TABLE 'topics' DB 'testdb')) LAYOUT(HASHED()) LIFETIME(300)",
    )
    return schema


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    """Set up a CLI runner in a tmp project directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "migrations" / "sql" / "snapshots").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("""
environments:
  dev:
    host: localhost
    database: testdb
    user: test
""")
    monkeypatch.setenv("CH_DEV_PASSWORD", "pass")
    return CliRunner()


class TestSnapshotCommand:
    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_captures_all_objects(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result = runner.invoke(main, ["snapshot", "dev"])
        assert result.exit_code == 0

        # Find the timestamped snapshot dir
        snapshots_dir = tmp_path / "migrations" / "sql" / "snapshots"
        snapshot_dirs = list(snapshots_dir.iterdir())
        assert len(snapshot_dirs) == 1

        snap = snapshot_dirs[0]
        assert re.match(r"\d{8}_\d{6}", snap.name)

        # Verify file structure
        assert (snap / "tables" / "users.sql").exists()
        assert (snap / "tables" / "events.sql").exists()
        assert (snap / "tables" / "peerdb_staging.sql").exists()
        assert (snap / "views" / "active_users.sql").exists()
        assert (snap / "materialized_views" / "hourly_events.sql").exists()
        assert (snap / "dictionaries" / "dict_topic.sql").exists()

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_ddl_content_written(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result = runner.invoke(main, ["snapshot", "dev"])
        assert result.exit_code == 0

        snapshots_dir = tmp_path / "migrations" / "sql" / "snapshots"
        snap = list(snapshots_dir.iterdir())[0]

        content = (snap / "tables" / "users.sql").read_text()
        assert "CREATE TABLE testdb.users" in content

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_exclude_filter(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result = runner.invoke(main, ["snapshot", "dev", "--exclude", "peerdb_*"])
        assert result.exit_code == 0

        snapshots_dir = tmp_path / "migrations" / "sql" / "snapshots"
        snap = list(snapshots_dir.iterdir())[0]

        assert (snap / "tables" / "users.sql").exists()
        assert not (snap / "tables" / "peerdb_staging.sql").exists()

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_exclude_comma_separated(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result = runner.invoke(main, ["snapshot", "dev", "--exclude", "peerdb_*,events"])
        assert result.exit_code == 0

        snapshots_dir = tmp_path / "migrations" / "sql" / "snapshots"
        snap = list(snapshots_dir.iterdir())[0]

        assert (snap / "tables" / "users.sql").exists()
        assert not (snap / "tables" / "peerdb_staging.sql").exists()
        assert not (snap / "tables" / "events.sql").exists()

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_include_filter(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result = runner.invoke(main, ["snapshot", "dev", "--filter", "user*,dict_*,*_users"])
        assert result.exit_code == 0

        snapshots_dir = tmp_path / "migrations" / "sql" / "snapshots"
        snap = list(snapshots_dir.iterdir())[0]

        assert (snap / "tables" / "users.sql").exists()
        assert (snap / "views" / "active_users.sql").exists()
        assert (snap / "dictionaries" / "dict_topic.sql").exists()
        assert not (snap / "tables" / "events.sql").exists()

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_filter_no_matches_exits_1(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result = runner.invoke(main, ["snapshot", "dev", "--filter", "nonexistent_*"])
        assert result.exit_code == 1
        assert "No objects matched" in result.output

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_timestamped_dirs_no_overwrite(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = _make_schema()

        result1 = runner.invoke(main, ["snapshot", "dev"])
        assert result1.exit_code == 0

        # Ensure a second snapshot creates a new directory (different timestamp)
        import time
        time.sleep(1.1)

        result2 = runner.invoke(main, ["snapshot", "dev"])
        assert result2.exit_code == 0

        snapshots_dir = tmp_path / "migrations" / "sql" / "snapshots"
        snapshot_dirs = list(snapshots_dir.iterdir())
        assert len(snapshot_dirs) == 2

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_empty_schema(self, mock_schema, mock_client, runner, tmp_path):
        mock_client.return_value = MagicMock()
        mock_schema.return_value = Schema(database="testdb")

        result = runner.invoke(main, ["snapshot", "dev"])
        assert result.exit_code == 1
        assert "No objects matched" in result.output


class TestSnapshotDisplay:
    def test_render_snapshot_progress(self):
        from io import StringIO
        from rich.console import Console
        from ch_migrate.display import render_snapshot_progress

        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        render_snapshot_progress(
            "migrations/sql/snapshots/20260305_120000",
            {"tables": 3, "views": 1, "materialized_views": 0, "dictionaries": 2},
            excluded=5,
            console=console,
        )

        text = output.getvalue()
        assert "Schema Snapshot" in text
        assert "6 objects captured" in text
        assert "5 excluded" in text

    def test_render_snapshot_progress_no_excluded(self):
        from io import StringIO
        from rich.console import Console
        from ch_migrate.display import render_snapshot_progress

        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        render_snapshot_progress(
            "migrations/sql/snapshots/20260305_120000",
            {"tables": 2, "views": 0, "materialized_views": 0, "dictionaries": 0},
            console=console,
        )

        text = output.getvalue()
        assert "2 objects captured" in text
        assert "excluded" not in text
