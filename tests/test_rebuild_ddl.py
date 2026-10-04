"""Semantic tests of rebuild helper schemas and rejection boundaries."""

from __future__ import annotations

import pytest

from ch_migrate.introspect import TableDefinition
from ch_migrate.rebuild_ddl import _parse, build_definition, dual_ddl, helper_ddl
from ch_migrate.rebuild_types import RebuildOptions

SOURCE = """CREATE TABLE `analytics`.`events` UUID 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' (
    `id` UInt64 CODEC(Delta, ZSTD(3)),
    `day` Date DEFAULT toDate('2026-10-04'),
    `kind` LowCardinality(String),
    `computed` UInt64 MATERIALIZED id * 2,
    `virtual` String ALIAS toString(id),
    INDEX ix_kind kind TYPE bloom_filter(0.01) GRANULARITY 2,
    PROJECTION by_day (SELECT day, count() GROUP BY day)
) ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)
PARTITION BY toYYYYMM(day) PRIMARY KEY id ORDER BY (id, day)
TTL day + INTERVAL 90 DAY SETTINGS index_granularity = 4096, allow_nullable_key = 0"""
TARGET = """CREATE TABLE analytics.events (
    `id` UInt64 CODEC(Delta, ZSTD(3)),
    `day` Date DEFAULT toDate('2026-10-04'),
    `kind` LowCardinality(String),
    `computed` UInt64 MATERIALIZED id * 2,
    `virtual` String ALIAS toString(id),
    INDEX ix_kind kind TYPE bloom_filter(0.01) GRANULARITY 2,
    PROJECTION by_day (SELECT day, count() GROUP BY day)
) ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)
PARTITION BY toYYYYMM(day) PRIMARY KEY id ORDER BY (day, id)
TTL day + INTERVAL 90 DAY SETTINGS index_granularity = 4096, allow_nullable_key = 0"""
UUIDS = {
    "snap": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "new": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    "stage": "dddddddd-dddd-dddd-dddd-dddddddddddd",
}


def _definition(target: str = TARGET, source: str = SOURCE, **overrides):
    options = RebuildOptions("analytics", "events", "r1", 3, **overrides)
    return build_definition(
        TableDefinition("events", "ReplicatedReplacingMergeTree", raw_ddl=source), target, options
    )


def test_helpers_preserve_complete_physical_schema_and_isolate_keeper_paths():
    definition = _definition(cluster="all_replicas")
    paths = set()
    for role, uuid in UUIDS.items():
        sql = helper_ddl(definition, role, uuid)
        parsed = _parse(sql, "analytics", "events__chm_" + role)
        assert parsed.uuid_span is not None
        assert sql[parsed.uuid_span[0] : parsed.uuid_span[1]] == f"UUID '{uuid}'"
        assert parsed.cluster_span is not None
        assert parsed.engine_args[1:] == ("'{replica}'", "id")
        assert parsed.clauses["PARTITION"] == "toYYYYMM(day)"
        assert parsed.clauses["PRIMARY"] == "id"
        assert parsed.clauses["TTL"] == "day + INTERVAL 90 DAY"
        assert parsed.clauses["SETTINGS"] == "index_granularity = 4096, allow_nullable_key = 0"
        assert "CODEC(Delta, ZSTD(3))" in sql
        assert "INDEX ix_kind" in sql
        assert "PROJECTION by_day" in sql
        assert "CREATE TABLE" in sql and " AS analytics.events" not in sql
        assert parsed.clauses["ORDER"] == ("(id, day)" if role == "snap" else "(day, id)")
        paths.add(parsed.engine_args[0])
    assert len(paths) == 3
    assert all("/__chm_" in path for path in paths)
    assert definition.columns == ("id", "day", "kind")
    assert definition.target.order_by == ["day", "id"]


def test_implicit_keeper_arguments_get_explicit_unique_paths_preserving_versions():
    source = SOURCE.replace(
        "ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)",
        "ReplicatedReplacingMergeTree(id)",
    )
    target = TARGET.replace(
        "ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)",
        "ReplicatedReplacingMergeTree(id)",
    )
    definition = _definition(target, source)
    paths = []
    for role, uuid in UUIDS.items():
        parsed = _parse(helper_ddl(definition, role, uuid), "analytics", "events__chm_" + role)
        assert parsed.engine_args == (f"'/clickhouse/tables/{uuid}/{{shard}}'", "'{replica}'", "id")
        paths.append(parsed.engine_args[0])
    assert len(set(paths)) == 3


def test_zero_arg_engine_and_nonreplicated_engine_do_not_share_keeper_paths():
    source = SOURCE.replace(
        "ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)",
        "ReplicatedMergeTree",
    )
    definition = _definition(
        TARGET.replace(
            "ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)",
            "ReplicatedMergeTree()",
        ),
        source,
    )
    assert _parse(
        helper_ddl(definition, "snap", UUIDS["snap"]), "analytics", "events__chm_snap"
    ).engine_args == (f"'/clickhouse/tables/{UUIDS['snap']}/{{shard}}'", "'{replica}'")
    plain = _definition(
        TARGET.replace(
            "ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)",
            "MergeTree()",
        ),
        SOURCE.replace(
            "ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/analytics/events', '{replica}', id)",
            "MergeTree()",
        ),
    )
    assert (
        _parse(helper_ddl(plain, "new", UUIDS["new"]), "analytics", "events__chm_new").engine_args
        == ()
    )


def test_dual_projection_explicitly_names_insertable_target_columns():
    definition = _definition(select="id, toDate(day) AS day, concat(kind, ',', kind)")
    sql = dual_ddl(definition, UUIDS["new"])
    assert "TO `analytics`.`events__chm_new` AS SELECT" in sql
    assert "(id) AS `id`, (toDate(day)) AS `day`, (concat(kind, ',', kind)) AS `kind`" in sql
    assert sql.endswith("FROM `analytics`.`events`")
    assert "computed" not in sql and "virtual" not in sql
    assert "SELECT `id`, `day`, `kind` FROM" in dual_ddl(_definition(), UUIDS["new"])


@pytest.mark.parametrize(
    "sql",
    [
        TARGET.replace("analytics.events", "other.events", 1),
        TARGET.replace("analytics.events", "analytics.other", 1),
        TARGET.replace("(\n    `id`", "AS source (\n    `id`", 1),
        TARGET + "; CREATE TABLE analytics.second (id UInt64) ENGINE = MergeTree ORDER BY id",
        TARGET.replace("ENGINE =", "AS source ENGINE =", 1),
        TARGET.replace("ENGINE =", "UUID 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' ENGINE =", 1),
        TARGET.replace("ENGINE =", "ENGINE = Distributed('cluster', db, table) SETTINGS x=1 --", 1),
    ],
)
def test_ambiguous_or_wrong_target_is_refused(sql):
    with pytest.raises(ValueError):
        _definition(sql)


@pytest.mark.parametrize(
    "projection",
    [
        "SELECT id, day, kind FROM another",
        "id, day FROM another, kind",
        "id, day",
        "id, day, kind; DROP TABLE analytics.events",
        "id, day AS other, kind",
    ],
)
def test_invalid_projection_is_refused(projection):
    with pytest.raises(ValueError):
        _definition(select=projection)


def test_unknown_role_and_invalid_uuid_are_refused():
    definition = _definition()
    with pytest.raises(ValueError):
        helper_ddl(definition, "old", UUIDS["snap"])
    with pytest.raises(ValueError):
        helper_ddl(definition, "snap", "not-a-uuid")


def test_helper_keeper_roots_survive_source_cleanup_and_table_rename():
    source = SOURCE.replace("/analytics/events", "/{database}/{table}")
    target = TARGET.replace("/analytics/events", "/{database}/{table}")
    definition = _definition(target, source)
    original_root = "/clickhouse/tables/{shard}/analytics/events"
    for role, uuid in UUIDS.items():
        parsed = _parse(helper_ddl(definition, role, uuid), "analytics", "events__chm_" + role)
        path = parsed.engine_args[0].strip("'")
        assert "{table}" not in path and "{database}" not in path
        assert path != original_root
        assert not path.startswith(original_root + "/")
