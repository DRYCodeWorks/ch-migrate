"""Behavioral checks for rebuild refusals and conservative writer inspection."""

from __future__ import annotations

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
        if "system.replicas" in sql:
            return _Result([("node", "/group", "r1", 1)])
        if "system.parts" in sql:
            return _Result([("node", "202601", "default", 9, 100)])
        if "system.disks" in sql:
            return _Result([("node", "default", 1000)])
        if "system.merge_tree_settings" in sql:
            return _Result([("node", "10")])
        if "system.asynchronous_inserts" in sql:
            return _Result([])
        if "system.processes" in sql:
            return _Result([])
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


def test_query_failure_is_not_silently_converted_to_empty_evidence():
    class Broken(_Client):
        def query(self, sql):
            if "query_log" in sql:
                raise PermissionError("No query_log privilege")
            return super().query(sql)

    with pytest.raises(PermissionError, match="query_log"):
        inspect_rebuild(Broken(), RebuildRequest("db", _table(), _table("new")))


def test_newly_queued_writer_without_log_evidence_requires_opt_in():
    class Queued(_Client):
        def query(self, sql):
            if "system.asynchronous_inserts" in sql:
                return _Result([("node", ["unlogged-async-query"])])
            return super().query(sql)

    client = Queued(writer_settings={"async_insert": "0", "wait_for_async_insert": "1"})
    refused = inspect_rebuild(client, RebuildRequest("db", _table(), _table("new")))
    assert _codes(refused)["writer_settings_unknown"].severity == "refusal"
    allowed = inspect_rebuild(
        client, RebuildRequest("db", _table(), _table("new"), allow_unacknowledged_async_loss=True)
    )
    assert _codes(allowed)["writer_settings_unknown"].severity == "warning"


@pytest.mark.parametrize("wait", ["0", "1"])
@pytest.mark.parametrize("logged", [False, True])
def test_active_queued_writer_uses_per_query_settings_before_log_flush(wait, logged):
    query_id = "q1" if logged else "waiting"

    class Waiting(_Client):
        def query(self, sql):
            if "system.asynchronous_inserts" in sql:
                return _Result([("node", [query_id])])
            if "system.processes" in sql:
                return _Result(
                    [
                        (
                            "node",
                            query_id,
                            "writer",
                            {"async_insert": "1", "wait_for_async_insert": wait},
                        )
                    ]
                )
            return super().query(sql)

    result = inspect_rebuild(
        Waiting(
            writer_settings={} if logged else {"async_insert": "0", "wait_for_async_insert": "1"}
        ),
        RebuildRequest("db", _table(), _table("new")),
    )
    assert "writer_settings_unknown" not in _codes(result)
    assert any(finding.severity == "refusal" for finding in result.findings) == (wait == "0")
    if wait == "0":
        assert _codes(result)["unacknowledged_async_writer"].severity == "refusal"


def test_another_hosts_process_cannot_prove_queued_writer_settings():
    class WrongHost(_Client):
        def query(self, sql):
            if "system.asynchronous_inserts" in sql:
                return _Result([("node", ["waiting"])])
            if "system.processes" in sql:
                return _Result(
                    [
                        (
                            "other",
                            "waiting",
                            "writer",
                            {"async_insert": "1", "wait_for_async_insert": "1"},
                        )
                    ]
                )
            return super().query(sql)

    result = inspect_rebuild(
        WrongHost(writer_settings={"async_insert": "0", "wait_for_async_insert": "1"}),
        RebuildRequest("db", _table(), _table("new")),
    )
    assert _codes(result)["writer_settings_unknown"].severity == "refusal"


def test_physical_helpers_need_distinct_keeper_paths_but_matching_engine_semantics():
    source = _table(engine="ReplicatedReplacingMergeTree('/source', 'r1', id)")
    target = _table("new", "ReplicatedReplacingMergeTree('/new', 'r2', id)")
    client = _Client(writer_settings={"async_insert": "0", "wait_for_async_insert": "1"})
    result = inspect_rebuild(
        client, RebuildRequest("db", source, target, transfer_pairs=((source, target),))
    )
    assert "transfer_structure_mismatch" not in _codes(result)
    target.engine = "ReplicatedReplacingMergeTree('/new', 'r2', other_version)"
    mismatch = inspect_rebuild(
        client, RebuildRequest("db", source, target, transfer_pairs=((source, target),))
    )
    assert _codes(mismatch)["transfer_structure_mismatch"].details["differences"] == ["engine"]


def test_safe_profile_does_not_prove_unlogged_queued_insert_is_acknowledged():
    class HiddenOverride(_Client):
        def query(self, sql):
            if "system.asynchronous_inserts" in sql:
                return _Result([("node", ["q1"])])
            if "system.settings_profile_elements" in sql:
                return _Result(
                    [
                        ("node", "writer_profile", None, None, "async_insert", "0", None),
                        ("node", "writer_profile", None, None, "wait_for_async_insert", "1", None),
                    ]
                )
            return super().query(sql)

    result = inspect_rebuild(
        HiddenOverride(writer_settings={}), RebuildRequest("db", _table(), _table("new"))
    )
    assert _codes(result)["writer_settings_unknown"].severity == "refusal"
