"""Behavioral checks for rebuild refusals and conservative writer inspection."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from ch_migrate.introspect import ColumnDefinition, TableDefinition
from ch_migrate.rebuild_preflight import RebuildRequest, inspect_rebuild


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _Client:
    def __init__(self, *, writer_settings=None, profile_value="0", mutation=False, shards=1):
        self.writer_settings = (
            writer_settings if writer_settings is not None else {"async_insert": "1"}
        )
        self.profile_value = profile_value
        self.mutation = mutation
        self.shards = shards
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        if "uniqExact(shard_num)" in sql:
            return _Result([(self.shards,)])
        if "engine = 'Distributed'" in sql:
            return _Result([])
        if "system.mutations" in sql:
            return _Result([("node", "mutation_1")] if self.mutation else [])
        if "system.parts" in sql:
            return _Result([("node", "202601", "default", 9, 100)])
        if "system.disks" in sql:
            return _Result([("node", "default", 1000)])
        if "system.merge_tree_settings" in sql:
            return _Result([("node", "10")])
        if "query_log" in sql:
            return _Result(
                [
                    (
                        "node",
                        "q1",
                        "QueryFinish",
                        "writer",
                        "db",
                        ["db.t"],
                        "INSERT INTO db.t VALUES (1)",
                        self.writer_settings,
                        3600,
                        "2026-01-01",
                    )
                ]
            )
        if "system.settings_profiles" in sql:
            return _Result([("node", "writer_profile", 0, ["writer"], [])])
        if "system.settings_profile_elements" in sql:
            return _Result(
                [
                    (
                        "node",
                        "writer_profile",
                        None,
                        None,
                        "wait_for_async_insert",
                        self.profile_value,
                        None,
                    )
                ]
            )
        if "system.users" in sql:
            return _Result([("node", "writer", 0, [], [])])
        if "system.role_grants" in sql:
            return _Result([])
        if "name, default FROM" in sql:
            return _Result([])
        if "system.settings " in sql:
            return _Result([("0",)])
        raise AssertionError(sql)


def _table(name="t", engine="MergeTree", partition="toYYYYMM(day)"):
    return TableDefinition(name, engine, [ColumnDefinition("id", "UInt64")], ["id"], partition)


def _codes(result):
    return {finding.code: finding for finding in result.findings}


def test_inherited_profile_refuses_unacknowledged_async_writer():
    client = _Client()
    result = inspect_rebuild(client, RebuildRequest("db", _table(), _table("new")))
    assert _codes(result)["unacknowledged_async_writer"].severity == "refusal"
    assert result.insert_rows_per_second == 1.0
    assert result.partition_parts == {"202601": 9}
    assert _codes(result)["many_parts"].severity == "warning"
    assert "query_log(_[0-9]+)?" in " ".join(client.queries)
    assert all(query.lstrip().upper().startswith("SELECT") for query in client.queries)
    assert asdict(result)["findings"][0]["severity"] in {"warning", "refusal"}


def test_unacknowledged_loss_requires_explicit_acknowledgement():
    result = inspect_rebuild(
        _Client(),
        RebuildRequest("db", _table(), _table("new"), allow_unacknowledged_async_loss=True),
    )
    assert _codes(result)["unacknowledged_async_writer"].severity == "warning"


def test_unknown_profile_setting_fails_closed():
    result = inspect_rebuild(
        _Client(profile_value=None), RebuildRequest("db", _table(), _table("new"))
    )
    assert _codes(result)["writer_settings_unknown"].severity == "refusal"


def test_transfer_pair_mismatch_not_intentional_row_copy():
    source = _table()
    replacement = _table("new", "ReplacingMergeTree")
    changed = _table("stage", "ReplicatedMergeTree")
    result = inspect_rebuild(
        _Client(writer_settings={"async_insert": "0", "wait_for_async_insert": "1"}),
        RebuildRequest("db", source, replacement, transfer_pairs=((replacement, changed),)),
    )
    assert _codes(result)["transfer_structure_mismatch"].details["differences"] == ["engine"]
    assert "ReplacingMergeTree-family" in result.engine_note
    assert "source_target_engine_mismatch" not in _codes(result)


def test_partition_key_and_mutation_and_shards_refuse():
    request = RebuildRequest("db", _table(), _table("new", partition="day"), cluster="replicas")
    result = inspect_rebuild(_Client(mutation=True, shards=2), request)
    codes = _codes(result)
    assert {"partition_key_change", "unfinished_mutations", "sharded_cluster"} <= codes.keys()
    assert all(
        codes[code].severity == "refusal"
        for code in ("partition_key_change", "unfinished_mutations", "sharded_cluster")
    )


def test_versioned_collapsing_note_does_not_promise_deduplication():
    target = _table("new", "VersionedCollapsingMergeTree")
    result = inspect_rebuild(
        _Client(writer_settings={"async_insert": "0", "wait_for_async_insert": "1"}),
        RebuildRequest("db", _table(), target),
    )
    assert "do not guarantee elimination of identical positive rows" in result.engine_note


def test_query_failure_is_not_silently_converted_to_empty_evidence():
    class Broken(_Client):
        def query(self, sql):
            if "query_log" in sql:
                raise PermissionError("No query_log privilege")
            return super().query(sql)

    with pytest.raises(PermissionError, match="query_log"):
        inspect_rebuild(Broken(), RebuildRequest("db", _table(), _table("new")))
