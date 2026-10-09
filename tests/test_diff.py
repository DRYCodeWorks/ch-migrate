"""Tests for schema diff module and CLI command."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from ch_migrate.diff import (
    DiffStatus,
    FieldDiff,
    SchemaDiff,
    compare_schemas,
)
from ch_migrate.display import render_diff_report
from ch_migrate.introspect import (
    ColumnDefinition,
    DictDefinition,
    MVDefinition,
    Schema,
    TableDefinition,
    ViewDefinition,
)


# ---------------------------------------------------------------------------
# compare_schemas unit tests
# ---------------------------------------------------------------------------


class TestCompareSchemas:
    def _base_table(self, name: str = "users", **overrides) -> TableDefinition:
        defaults = dict(
            name=name,
            engine="MergeTree",
            columns=[
                ColumnDefinition(name="id", type="UInt64"),
                ColumnDefinition(name="name", type="String"),
            ],
            order_by=["id"],
            partition_by=None,
            ttl=None,
            settings={},
            raw_ddl=f"CREATE TABLE {name} (id UInt64, name String) ENGINE = MergeTree ORDER BY id",
        )
        defaults.update(overrides)
        return TableDefinition(**defaults)

    def test_identical_schemas_in_sync(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table()
        live.tables["users"] = self._base_table()

        diffs = compare_schemas(local, live)
        assert len(diffs) == 1
        assert diffs[0].status == DiffStatus.IN_SYNC

    def test_local_only_table(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table()

        diffs = compare_schemas(local, live)
        assert len(diffs) == 1
        assert diffs[0].status == DiffStatus.LOCAL_ONLY
        assert diffs[0].name == "users"

    def test_remote_only_table(self):
        local = Schema(database="db")
        live = Schema(database="db")
        live.tables["users"] = self._base_table()

        diffs = compare_schemas(local, live)
        assert len(diffs) == 1
        assert diffs[0].status == DiffStatus.REMOTE_ONLY
        assert diffs[0].name == "users"

    def test_detects_added_column(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table()
        live.tables["users"] = self._base_table(columns=[
            ColumnDefinition(name="id", type="UInt64"),
            ColumnDefinition(name="name", type="String"),
            ColumnDefinition(name="email", type="String"),
        ])

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("email" in fd.message and "in the database but not in the snapshot" in fd.message for fd in modified[0].field_diffs)

    def test_detects_removed_column(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table(columns=[
            ColumnDefinition(name="id", type="UInt64"),
            ColumnDefinition(name="name", type="String"),
            ColumnDefinition(name="phone", type="String"),
        ])
        live.tables["users"] = self._base_table()

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("phone" in fd.message and "in the snapshot but not in the database" in fd.message for fd in modified[0].field_diffs)

    def test_detects_column_type_change(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table()
        live.tables["users"] = self._base_table(columns=[
            ColumnDefinition(name="id", type="UInt64"),
            ColumnDefinition(name="name", type="Nullable(String)"),
        ])

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("type differs" in fd.message for fd in modified[0].field_diffs)

    def test_detects_column_codec_change(self):
        """Codec changes on a column should be detected as drift."""
        local_schema = Schema(
            database="test",
            tables={
                "logs": TableDefinition(
                    name="logs",
                    engine="MergeTree",
                    columns=[
                        ColumnDefinition(name="body", type="String", codec="ZSTD(1)"),
                    ],
                    order_by=["id"],
                ),
            },
        )
        remote_schema = Schema(
            database="test",
            tables={
                "logs": TableDefinition(
                    name="logs",
                    engine="MergeTree",
                    columns=[
                        ColumnDefinition(name="body", type="String", codec="ZSTD(3)"),
                    ],
                    order_by=["id"],
                ),
            },
        )
        diffs = compare_schemas(local_schema, remote_schema)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("codec" in f.field_name for f in modified[0].field_diffs)

    def test_detects_engine_change(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table(engine="MergeTree")
        live.tables["users"] = self._base_table(engine="ReplacingMergeTree(version)")

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("engine differs" in fd.message for fd in modified[0].field_diffs)

    def test_detects_order_by_change(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table(order_by=["id"])
        live.tables["users"] = self._base_table(order_by=["id", "name"])

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("ORDER BY" in fd.message for fd in modified[0].field_diffs)

    def test_detects_partition_by_change(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.tables["users"] = self._base_table(partition_by=None)
        live.tables["users"] = self._base_table(partition_by="toYYYYMM(created_at)")

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("PARTITION BY" in fd.message for fd in modified[0].field_diffs)

    def test_views_fallback_to_raw_ddl(self):
        local = Schema(database="db")
        live = Schema(database="db")
        local.views["v"] = ViewDefinition(name="v", select_query="SELECT 1", raw_ddl="CREATE VIEW v AS SELECT 1")
        live.views["v"] = ViewDefinition(name="v", select_query="SELECT 2", raw_ddl="CREATE VIEW v AS SELECT 2")

        diffs = compare_schemas(local, live)
        modified = [d for d in diffs if d.status == DiffStatus.MODIFIED]
        assert len(modified) == 1
        assert any("raw comparison" in fd.message for fd in modified[0].field_diffs)

    def test_views_in_sync(self):
        local = Schema(database="db")
        live = Schema(database="db")
        ddl = "CREATE VIEW v AS SELECT 1"
        local.views["v"] = ViewDefinition(name="v", select_query="SELECT 1", raw_ddl=ddl)
        live.views["v"] = ViewDefinition(name="v", select_query="SELECT 1", raw_ddl=ddl)

        diffs = compare_schemas(local, live)
        assert all(d.status == DiffStatus.IN_SYNC for d in diffs)

    def test_mixed_object_types(self):
        local = Schema(database="db")
        live = Schema(database="db")

        local.tables["t1"] = self._base_table("t1")
        live.tables["t1"] = self._base_table("t1")

        local.views["v1"] = ViewDefinition(name="v1", select_query="SELECT 1", raw_ddl="CREATE VIEW v1 AS SELECT 1")
        # v1 is local_only (not in live)

        live.dictionaries["d1"] = DictDefinition(name="d1", raw_ddl="CREATE DICTIONARY d1 (...)")
        # d1 is remote_only

        diffs = compare_schemas(local, live)
        statuses = {d.name: d.status for d in diffs}
        assert statuses["t1"] == DiffStatus.IN_SYNC
        assert statuses["v1"] == DiffStatus.LOCAL_ONLY
        assert statuses["d1"] == DiffStatus.REMOTE_ONLY


# ---------------------------------------------------------------------------
# CLI diff command tests
# ---------------------------------------------------------------------------


@pytest.fixture
def diff_runner(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("""
environments:
  dev:
    host: localhost
    database: testdb
    user: test
""")
    monkeypatch.setenv("CH_DEV_PASSWORD", "pass")

    # Create a snapshot
    snap_dir = tmp_path / "migrations" / "sql" / "snapshots" / "20260305_120000"
    tables_dir = snap_dir / "tables"
    tables_dir.mkdir(parents=True)
    (tables_dir / "users.sql").write_text(
        "CREATE TABLE testdb.users (id UInt64, name String) ENGINE = MergeTree ORDER BY id"
    )
    return CliRunner()


class TestDiffCommand:
    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_in_sync_exits_0(self, mock_schema, mock_client, diff_runner, tmp_path):
        mock_client.return_value = MagicMock()
        live = Schema(database="testdb")
        live.tables["users"] = TableDefinition(
            name="users",
            engine="MergeTree",
            columns=[
                ColumnDefinition(name="id", type="UInt64"),
                ColumnDefinition(name="name", type="String"),
            ],
            order_by=["id"],
            raw_ddl="CREATE TABLE testdb.users (id UInt64, name String) ENGINE = MergeTree ORDER BY id",
        )
        mock_schema.return_value = live

        from ch_migrate.cli import main
        result = diff_runner.invoke(main, ["diff", "dev"])
        assert result.exit_code == 0

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_drift_exits_1(self, mock_schema, mock_client, diff_runner, tmp_path):
        mock_client.return_value = MagicMock()
        live = Schema(database="testdb")
        live.tables["users"] = TableDefinition(
            name="users",
            engine="ReplacingMergeTree",
            columns=[
                ColumnDefinition(name="id", type="UInt64"),
                ColumnDefinition(name="name", type="String"),
            ],
            order_by=["id"],
            raw_ddl="CREATE TABLE testdb.users (id UInt64, name String) ENGINE = ReplacingMergeTree ORDER BY id",
        )
        mock_schema.return_value = live

        from ch_migrate.cli import main
        result = diff_runner.invoke(main, ["diff", "dev"])
        assert result.exit_code == 1

    @patch("ch_migrate.connection.get_client")
    @patch("ch_migrate.introspect.get_live_schema")
    def test_no_snapshot_exits_1(self, mock_schema, mock_client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("""
environments:
  dev:
    host: localhost
    database: testdb
    user: test
""")
        monkeypatch.setenv("CH_DEV_PASSWORD", "pass")

        from ch_migrate.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["diff", "dev"])
        assert result.exit_code == 1
        assert "No snapshots found" in result.output


# ---------------------------------------------------------------------------
# Display rendering tests
# ---------------------------------------------------------------------------


class TestDiffDisplay:
    def test_render_all_in_sync(self):
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        diffs = [SchemaDiff(name="users", obj_type="table", status=DiffStatus.IN_SYNC)]
        render_diff_report(diffs, console=console)

        text = output.getvalue()
        assert "in sync" in text

    def test_render_with_drift(self):
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        diffs = [
            SchemaDiff(name="users", obj_type="table", status=DiffStatus.MODIFIED, field_diffs=[
                FieldDiff("engine", "MergeTree", "ReplacingMergeTree", "engine differs"),
            ]),
            SchemaDiff(name="new_table", obj_type="table", status=DiffStatus.REMOTE_ONLY),
            SchemaDiff(name="old_view", obj_type="view", status=DiffStatus.LOCAL_ONLY),
        ]
        render_diff_report(diffs, console=console)

        text = output.getvalue()
        # Every drifted object is named, with what drifted.
        assert "users" in text and "engine differs" in text
        assert "new_table" in text and "in the database but not in the snapshot" in text
        assert "old_view" in text and "in the snapshot but not in the database" in text
