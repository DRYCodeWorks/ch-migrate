"""Tests for ClickHouse schema introspection and DDL parsing."""

from __future__ import annotations

import pytest

from ch_migrate.introspect import (
    ColumnDefinition,
    DependencyGraph,
    DictDefinition,
    MVDefinition,
    ObjectNode,
    Schema,
    TableDefinition,
    ViewDefinition,
    parse_create_dictionary,
    parse_create_mv,
    parse_create_statement,
    parse_create_table,
    parse_create_view,
)


# ---------------------------------------------------------------------------
# Sample DDL fixtures
# ---------------------------------------------------------------------------

MERGETREE_DDL = """\
CREATE TABLE mydb.users
(
    `id` UInt64,
    `name` String,
    `email` String,
    `created_at` DateTime DEFAULT now(),
    `score` Float64 CODEC(Delta, ZSTD(3)),
    `bio` String COMMENT 'User biography'
)
ENGINE = MergeTree
ORDER BY (id, created_at)
PARTITION BY toYYYYMM(created_at)
TTL created_at + INTERVAL 1 YEAR
SETTINGS index_granularity = 8192
"""

REPLACING_MERGETREE_DDL = """\
CREATE TABLE mydb.events
(
    `event_id` UInt64,
    `user_id` UInt64,
    `event_type` String,
    `payload` String,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (event_id)
"""

SHARED_MERGETREE_DDL = """\
CREATE TABLE mydb.geo_indicators
(
    `geo_layer` String,
    `topic_key` String,
    `start_year` UInt16,
    `geo_id` String,
    `stratification_key` String,
    `region_label` String,
    `time_period` String,
    `value` Nullable(Float64),
    `moe` Nullable(Float64),
    `states` Array(String)
)
ENGINE = SharedReplacingMergeTree('/clickhouse/tables/{uuid}', '{server}')
ORDER BY (geo_layer, topic_key, start_year, geo_id, stratification_key, region_label, time_period)
SETTINGS index_granularity = 8192
"""

TABLE_WITH_MATERIALIZED_COL = """\
CREATE TABLE mydb.computed
(
    `id` UInt64,
    `raw_value` Float64,
    `doubled` Float64 MATERIALIZED raw_value * 2,
    `label` String ALIAS concat('item-', toString(id))
)
ENGINE = MergeTree
ORDER BY id
"""

VIEW_DDL = """\
CREATE VIEW mydb.active_users AS
SELECT id, name, email
FROM mydb.users
WHERE score > 0
"""

MV_WITH_TO_DDL = """\
CREATE MATERIALIZED VIEW mydb.hourly_events
TO mydb.hourly_events_dest
AS SELECT
    toStartOfHour(event_time) AS hour,
    event_type,
    count() AS cnt
FROM mydb.events
GROUP BY hour, event_type
"""

MV_WITH_ENGINE_DDL = """\
CREATE MATERIALIZED VIEW mydb.daily_stats
ENGINE = SummingMergeTree
ORDER BY (day, metric)
AS SELECT
    toDate(event_time) AS day,
    metric,
    sum(value) AS total
FROM mydb.raw_metrics
GROUP BY day, metric
"""

MV_WITH_JOIN_DDL = """\
CREATE MATERIALIZED VIEW mydb.enriched_events
TO mydb.enriched_dest
AS SELECT
    e.event_id,
    e.event_type,
    u.name AS user_name
FROM mydb.events AS e
JOIN mydb.users AS u ON e.user_id = u.id
"""

DICT_TABLE_SOURCE_DDL = """\
CREATE DICTIONARY mydb.dict_topic
(
    `key` String,
    `name` String,
    `units` String
)
PRIMARY KEY key
SOURCE(CLICKHOUSE(TABLE 'public_attributes_attribute' DB 'mydb'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(MIN 300 MAX 600)
"""

DICT_QUERY_SOURCE_DDL = """\
CREATE DICTIONARY mydb.dict_topic_category
(
    `topic_key` String,
    `category_name` String
)
PRIMARY KEY topic_key
SOURCE(CLICKHOUSE(QUERY 'SELECT a.key AS topic_key, c.name AS category_name FROM mydb.attributes AS a JOIN mydb.categories AS c ON a.cat_id = c.id'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(MIN 300 MAX 600)
"""

TABLE_ON_CLUSTER_DDL = """\
CREATE TABLE mydb.replicated_data ON CLUSTER default
(
    `id` UInt64,
    `value` String
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/replicated_data', '{replica}')
ORDER BY id
"""

TABLE_IF_NOT_EXISTS_DDL = """\
CREATE TABLE IF NOT EXISTS mydb.safe_table
(
    `id` UInt64,
    `name` String
)
ENGINE = MergeTree
ORDER BY id
"""


# ---------------------------------------------------------------------------
# Table parsing tests
# ---------------------------------------------------------------------------


class TestParseCreateTable:
    def test_mergetree_basic(self):
        result = parse_create_table(MERGETREE_DDL)
        assert result is not None
        assert result.name == "users"
        assert result.engine == "MergeTree"
        assert result.order_by == ["id", "created_at"]
        assert result.partition_by == "toYYYYMM(created_at)"
        assert result.ttl == "created_at + INTERVAL 1 YEAR"
        assert result.settings == {"index_granularity": "8192"}

    def test_mergetree_columns(self):
        result = parse_create_table(MERGETREE_DDL)
        assert result is not None
        assert len(result.columns) == 6

        id_col = result.columns[0]
        assert id_col.name == "id"
        assert id_col.type == "UInt64"

        created_col = result.columns[3]
        assert created_col.name == "created_at"
        assert created_col.default_kind == "DEFAULT"
        assert created_col.default_expr == "now()"

        score_col = result.columns[4]
        assert score_col.name == "score"
        assert score_col.codec == "Delta, ZSTD(3)"

        bio_col = result.columns[5]
        assert bio_col.name == "bio"
        assert bio_col.comment == "User biography"

    def test_replacing_mergetree(self):
        result = parse_create_table(REPLACING_MERGETREE_DDL)
        assert result is not None
        assert result.name == "events"
        assert result.engine == "ReplacingMergeTree(version)"
        assert result.order_by == ["event_id"]
        assert result.partition_by is None
        assert result.ttl is None

    def test_shared_mergetree(self):
        result = parse_create_table(SHARED_MERGETREE_DDL)
        assert result is not None
        assert result.name == "geo_indicators"
        assert "SharedReplacingMergeTree" in result.engine
        assert len(result.order_by) == 7
        assert result.order_by[0] == "geo_layer"

    def test_materialized_and_alias_columns(self):
        result = parse_create_table(TABLE_WITH_MATERIALIZED_COL)
        assert result is not None

        doubled = result.columns[2]
        assert doubled.name == "doubled"
        assert doubled.default_kind == "MATERIALIZED"
        assert doubled.default_expr == "raw_value * 2"

        label = result.columns[3]
        assert label.name == "label"
        assert label.default_kind == "ALIAS"

    def test_on_cluster(self):
        result = parse_create_table(TABLE_ON_CLUSTER_DDL)
        assert result is not None
        assert result.name == "replicated_data"
        assert "ReplicatedMergeTree" in result.engine

    def test_if_not_exists(self):
        result = parse_create_table(TABLE_IF_NOT_EXISTS_DDL)
        assert result is not None
        assert result.name == "safe_table"

    def test_raw_ddl_preserved(self):
        result = parse_create_table(MERGETREE_DDL)
        assert result is not None
        assert result.raw_ddl == MERGETREE_DDL

    def test_unparseable_returns_none(self):
        assert parse_create_table("SELECT 1") is None
        assert parse_create_table("") is None


# ---------------------------------------------------------------------------
# View parsing tests
# ---------------------------------------------------------------------------


class TestParseCreateView:
    def test_basic_view(self):
        result = parse_create_view(VIEW_DDL)
        assert result is not None
        assert result.name == "active_users"
        assert "SELECT id, name, email" in result.select_query
        assert "FROM mydb.users" in result.select_query

    def test_unparseable_returns_none(self):
        assert parse_create_view("CREATE TABLE foo (id UInt64) ENGINE = MergeTree") is None


# ---------------------------------------------------------------------------
# MV parsing tests
# ---------------------------------------------------------------------------


class TestParseCreateMV:
    def test_mv_with_to_table(self):
        result = parse_create_mv(MV_WITH_TO_DDL)
        assert result is not None
        assert result.name == "hourly_events"
        assert result.target_table == "mydb.hourly_events_dest"
        assert "events" in result.source_tables[0]
        assert "SELECT" in result.select_query

    def test_mv_with_engine(self):
        result = parse_create_mv(MV_WITH_ENGINE_DDL)
        assert result is not None
        assert result.name == "daily_stats"
        assert result.engine == "SummingMergeTree"
        assert any("raw_metrics" in s for s in result.source_tables)

    def test_mv_with_join_extracts_multiple_sources(self):
        result = parse_create_mv(MV_WITH_JOIN_DDL)
        assert result is not None
        assert result.name == "enriched_events"
        source_names = [s.split(".")[-1] for s in result.source_tables]
        assert "events" in source_names
        assert "users" in source_names

    def test_mv_ignores_function_calls_as_sources(self):
        """Functions like JSONAllPaths() should not appear as source tables."""
        ddl = (
            "CREATE MATERIALIZED VIEW default.attrs_mv TO default.attrs AS "
            "SELECT path, project_id "
            "FROM default.logs ARRAY JOIN JSONAllPaths(log_attributes_json) AS path"
        )
        mv = parse_create_mv(ddl)
        assert mv is not None
        assert "default.logs" in mv.source_tables
        # JSONAllPaths is a function, not a table
        assert all("JSONAllPaths" not in t for t in mv.source_tables)

    def test_unparseable_returns_none(self):
        assert parse_create_mv("CREATE TABLE foo (id UInt64) ENGINE = MergeTree") is None


# ---------------------------------------------------------------------------
# Dictionary parsing tests
# ---------------------------------------------------------------------------


class TestParseCreateDictionary:
    def test_table_source(self):
        result = parse_create_dictionary(DICT_TABLE_SOURCE_DDL)
        assert result is not None
        assert result.name == "dict_topic"
        assert result.primary_key == "key"
        assert result.source_type == "clickhouse"
        assert result.source_table == "public_attributes_attribute"
        assert result.source_db == "mydb"
        assert result.layout == "COMPLEX_KEY_HASHED"
        assert result.lifetime is not None

    def test_query_source(self):
        result = parse_create_dictionary(DICT_QUERY_SOURCE_DDL)
        assert result is not None
        assert result.name == "dict_topic_category"
        assert result.source_type == "clickhouse"
        assert result.source_query is not None
        assert "attributes" in result.source_query
        assert result.source_table is None

    def test_unparseable_returns_none(self):
        assert parse_create_dictionary("CREATE TABLE foo (id UInt64)") is None


# ---------------------------------------------------------------------------
# parse_create_statement dispatch tests
# ---------------------------------------------------------------------------


class TestParseCreateStatement:
    def test_dispatches_to_table(self):
        result = parse_create_statement(MERGETREE_DDL)
        assert isinstance(result, TableDefinition)
        assert result.name == "users"

    def test_dispatches_to_view(self):
        result = parse_create_statement(VIEW_DDL)
        assert isinstance(result, ViewDefinition)
        assert result.name == "active_users"

    def test_dispatches_to_mv(self):
        result = parse_create_statement(MV_WITH_TO_DDL)
        assert isinstance(result, MVDefinition)
        assert result.name == "hourly_events"

    def test_dispatches_to_dictionary(self):
        result = parse_create_statement(DICT_TABLE_SOURCE_DDL)
        assert isinstance(result, DictDefinition)
        assert result.name == "dict_topic"

    def test_unparseable_returns_none(self):
        assert parse_create_statement("DROP TABLE foo") is None
        assert parse_create_statement("ALTER TABLE foo ADD COLUMN bar UInt64") is None


# ---------------------------------------------------------------------------
# DependencyGraph tests
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def _make_graph(self) -> DependencyGraph:
        from ch_migrate.introspect import DepType, DependencyEdge

        graph = DependencyGraph()
        graph.nodes = {
            "users": ObjectNode(name="users", obj_type="table"),
            "events": ObjectNode(name="events", obj_type="table"),
            "hourly_events": ObjectNode(name="hourly_events", obj_type="materialized_view"),
            "dict_users": ObjectNode(name="dict_users", obj_type="dictionary"),
        }
        graph.edges = [
            DependencyEdge(source="events", target="hourly_events", dep_type=DepType.DATA_FLOW),
            DependencyEdge(source="events", target="hourly_events", dep_type=DepType.SCHEMA),
            DependencyEdge(source="users", target="dict_users", dep_type=DepType.SCHEMA),
        ]
        return graph

    def test_topological_order(self):
        graph = self._make_graph()
        order = graph.topological_order()
        # Source tables should come before dependents
        assert order.index("events") < order.index("hourly_events")
        assert order.index("users") < order.index("dict_users")

    def test_affected_by_drop(self):
        graph = self._make_graph()
        affected = graph.affected_by_drop("events")
        affected_names = [n.name for n in affected]
        assert "hourly_events" in affected_names

    def test_affected_by_drop_deduplicates(self):
        """Multi-edge pairs (data_flow + schema) should not produce duplicates."""
        graph = self._make_graph()
        # events -> hourly_events has both data_flow and schema edges
        affected = graph.affected_by_drop("events")
        affected_names = [n.name for n in affected]
        assert affected_names == ["hourly_events"]  # no duplicate

    def test_affected_by_drop_no_deps(self):
        graph = self._make_graph()
        affected = graph.affected_by_drop("hourly_events")
        assert affected == []


# ---------------------------------------------------------------------------
# Schema model tests
# ---------------------------------------------------------------------------


class TestSchema:
    def test_empty_schema(self):
        schema = Schema(database="test_db")
        assert schema.tables == {}
        assert schema.views == {}
        assert schema.materialized_views == {}
        assert schema.dictionaries == {}
        assert schema.database == "test_db"
        assert schema.ch_version is None

    def test_schema_populated(self):
        schema = Schema(database="mydb", ch_version="24.3.1")
        schema.tables["users"] = parse_create_table(MERGETREE_DDL)
        schema.views["active_users"] = parse_create_view(VIEW_DDL)
        schema.materialized_views["hourly_events"] = parse_create_mv(MV_WITH_TO_DDL)
        schema.dictionaries["dict_topic"] = parse_create_dictionary(DICT_TABLE_SOURCE_DDL)

        assert len(schema.tables) == 1
        assert len(schema.views) == 1
        assert len(schema.materialized_views) == 1
        assert len(schema.dictionaries) == 1
        assert schema.ch_version == "24.3.1"
