"""Semantic tests of rebuild helper schemas and rejection boundaries."""

from __future__ import annotations

import pytest

from ch_migrate.introspect import TableDefinition
from ch_migrate.rebuild_ddl import (
    _parse,
    build_definition,
    dual_ddl,
    helper_ddl,
    target_comment,
)
from ch_migrate.rebuild_types import HelperIdentity, RebuildOptions

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
        sql = helper_ddl(definition, role, HelperIdentity(uuid=uuid))
        parsed = _parse(sql, "analytics", "events__chm_" + role)
        assert parsed.uuid_span is not None
        assert sql[parsed.uuid_span[0] : parsed.uuid_span[1]] == f"UUID '{uuid}'"
        assert parsed.cluster_span is not None
        assert parsed.clauses["PARTITION"] == "toYYYYMM(day)"
        assert parsed.clauses["PRIMARY"] == "id"
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


def test_snapshot_and_stage_are_plain_and_ttl_free_so_merges_keep_every_row():
    source = SOURCE.replace(
        "`kind` LowCardinality(String),", "`kind` LowCardinality(String) TTL day + INTERVAL 1 DAY,"
    )
    definition = _definition(
        TARGET.replace(
            "`kind` LowCardinality(String),",
            "`kind` LowCardinality(String) TTL day + INTERVAL 1 DAY CODEC(ZSTD(1)),",
        ),
        source,
    )
    for role, uuid in UUIDS.items():
        parsed = _parse(
            helper_ddl(definition, role, HelperIdentity(uuid=uuid)),
            "analytics",
            "events__chm_" + role,
        )
        kind = next(column for column in parsed.columns if column.name == "kind")
        if role == "new":
            assert parsed.engine_name == "ReplicatedReplacingMergeTree"
            assert parsed.engine_args[1:] == ("'{replica}'", "id")
            assert parsed.clauses["TTL"] == "day + INTERVAL 90 DAY"
            assert "TTL" in parsed.sql[parsed.schema_at : parsed.schema_end]
            continue
        assert parsed.engine_name == "ReplicatedMergeTree"
        assert parsed.engine_args[1:] == ("'{replica}'",)
        assert "TTL" not in parsed.clauses
        assert "TTL" not in parsed.sql[parsed.schema_at : parsed.schema_end]
        assert kind.type == "LowCardinality(String)"
        assert kind.codec == ("(ZSTD(1))" if role == "stage" else None)
        assert parsed.clauses["SETTINGS"] == "index_granularity = 4096, allow_nullable_key = 0"


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
        parsed = _parse(
            helper_ddl(definition, role, HelperIdentity(uuid=uuid)),
            "analytics",
            "events__chm_" + role,
        )
        params = ("id",) if role == "new" else ()
        assert parsed.engine_args == (
            f"'/clickhouse/tables/{uuid}/{{shard}}'",
            "'{replica}'",
            *params,
        )
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
        helper_ddl(definition, "snap", HelperIdentity(uuid=UUIDS["snap"])),
        "analytics",
        "events__chm_snap",
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
        _parse(
            helper_ddl(plain, "new", HelperIdentity(uuid=UUIDS["new"])),
            "analytics",
            "events__chm_new",
        ).engine_args
        == ()
    )


def test_dual_projection_explicitly_names_insertable_target_columns():
    definition = _definition(select="id, toDate(day) AS day, concat(kind, ',', kind)")
    sql = dual_ddl(definition, HelperIdentity(uuid=UUIDS["new"]))
    assert "TO `analytics`.`events__chm_new` AS SELECT" in sql
    assert "(id) AS `id`, (toDate(day)) AS `day`, (concat(kind, ',', kind)) AS `kind`" in sql
    assert sql.endswith("FROM `analytics`.`events`")
    assert "computed" not in sql and "virtual" not in sql
    assert "SELECT `id`, `day`, `kind` FROM" in dual_ddl(
        _definition(), HelperIdentity(uuid=UUIDS["new"])
    )


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
        helper_ddl(definition, "old", HelperIdentity(uuid=UUIDS["snap"]))
    with pytest.raises(ValueError):
        helper_ddl(definition, "snap", HelperIdentity(uuid="not-a-uuid"))


def test_helper_keeper_roots_survive_source_cleanup_and_table_rename():
    source = SOURCE.replace("/analytics/events", "/{database}/{table}")
    target = TARGET.replace("/analytics/events", "/{database}/{table}")
    definition = _definition(target, source)
    original_root = "/clickhouse/tables/{shard}/analytics/events"
    for role, uuid in UUIDS.items():
        parsed = _parse(
            helper_ddl(definition, role, HelperIdentity(uuid=uuid)),
            "analytics",
            "events__chm_" + role,
        )
        path = parsed.engine_args[0].strip("'")
        assert "{table}" not in path and "{database}" not in path
        assert path != original_root
        assert not path.startswith(original_root + "/")


# ClickHouse Cloud reports Shared engines with its own Keeper arguments.
CLOUD_SOURCE = """CREATE TABLE analytics.events
(
    `id` UInt64,
    `v` UInt64,
    `day` Date TTL day + toIntervalYear(1)
)
ENGINE = SharedReplacingMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}', v)
PARTITION BY toYYYYMM(day)
ORDER BY id
TTL day + toIntervalYear(2)
SETTINGS index_granularity = 8192
COMMENT 'live events'"""


def _cloud_definition():
    target = CLOUD_SOURCE.replace("ORDER BY id", "ORDER BY (day, id)")
    return _definition(target, CLOUD_SOURCE)


def test_cloud_helpers_carry_owner_token_instead_of_a_chosen_uuid():
    definition = _cloud_definition()
    for role in ("new", "snap", "stage"):
        token = f"chm-rebuild:r1:{role}:abc"
        sql = helper_ddl(definition, role, HelperIdentity(token=token))
        parsed = _parse(sql, "analytics", "events__chm_" + role)
        assert parsed.uuid_span is None and "UUID" not in sql
        assert parsed.clauses["COMMENT"] == f"'{token}'"
        assert "live events" not in sql
        if role == "new":
            assert parsed.engine_name == "ReplacingMergeTree" and parsed.engine_args == ("v",)
            assert parsed.clauses["TTL"] == "day + toIntervalYear(2)"
        else:
            assert parsed.engine_name == "MergeTree" and parsed.engine_args == ()
            assert "TTL" not in sql


def test_cloud_dual_view_puts_owner_token_before_its_select():
    sql = dual_ddl(_cloud_definition(), HelperIdentity(token="chm-rebuild:r1:dual:abc"))
    assert "UUID" not in sql
    assert "TO `analytics`.`events__chm_new` COMMENT 'chm-rebuild:r1:dual:abc' AS SELECT" in sql


def test_target_comment_restores_the_replacements_own_comment():
    sql, value = target_comment(_cloud_definition())
    assert sql == "ALTER TABLE `analytics`.`events__chm_new` MODIFY COMMENT 'live events'"
    assert value == "live events"
    assert target_comment(_definition())[1] == ""
