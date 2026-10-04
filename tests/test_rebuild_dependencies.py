"""Behavioral boundaries for dependent discovery, replacement and validation."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from ch_migrate.introspect import parse_create_dictionary
from ch_migrate.rebuild_dependents import (
    DependentInventory,
    DependentQuery,
    DependentValidationError,
    DictionaryReload,
    _dictionary_query,
    _rewrite,
    _RewriteTarget,
    inspect_dependents,
    reload_dictionaries,
    validate_dependents,
)


@dataclass
class _Result:
    result_rows: tuple = ()
    column_names: tuple[str, ...] = ()
    column_types: tuple = ()


class _Client:
    def __init__(self, headers=None):
        self.sql = []
        self.headers = headers or {}

    def query(self, sql, parameters=None):
        self.sql.append(sql)
        if sql.startswith("EXPLAIN"):
            return _Result()
        if sql.startswith("SELECT * FROM (SELECT * FROM ") and "__chm_new" in sql:
            names = self.headers.get("target", {})
            return _Result(column_names=tuple(names), column_types=tuple(names.values()))
        if sql.startswith("SELECT * FROM ("):
            names = self.headers.get("source", {})
            return _Result(column_names=tuple(names), column_types=tuple(names.values()))
        return _Result()

    def command(self, sql):
        self.sql.append(sql)


def test_rewrite_changes_only_same_database_relation_not_literals_comments_or_alias():
    sql = (
        "SELECT 'FROM events', events.id FROM events AS events "
        "JOIN `analytics`.`events` e ON e.id = events.id "
        "JOIN other.events x ON x.id = e.id -- FROM events\n"
        "WHERE events.id > 0 /* JOIN events */"
    )
    rewritten, count = _rewrite(sql, _RewriteTarget("analytics", "events", "events__chm_new"))
    assert count == 2
    assert "FROM `analytics`.`events__chm_new` AS events" in rewritten
    assert "JOIN `analytics`.`events__chm_new` e" in rewritten
    assert "JOIN other.events x" in rewritten
    assert "'FROM events'" in rewritten
    assert "events.id > 0 /* JOIN events */" in rewritten


def test_dictionary_uses_declared_key_and_attributes_and_excludes_remote_sources():
    ddl = (
        "CREATE DICTIONARY analytics.lookup (`id` UInt64, `label` String, "
        "`weight` Float64) PRIMARY KEY id SOURCE(CLICKHOUSE("
        "HOST 'localhost' DB 'analytics' TABLE 'events')) "
        "LIFETIME(MIN 0 MAX 0) LAYOUT(HASHED())"
    )
    dictionary = parse_create_dictionary(ddl)
    assert _dictionary_query(dictionary, "analytics", "events") == (
        "SELECT `id`, `label`, `weight` FROM `analytics`.`events`"
    )
    assert (
        _dictionary_query(
            parse_create_dictionary(ddl.replace("localhost", "remote")), "analytics", "events"
        )
        is None
    )
    assert (
        _dictionary_query(
            parse_create_dictionary(ddl.replace("DB 'analytics'", "DB 'other'")),
            "analytics",
            "events",
        )
        is None
    )
    assert (
        _dictionary_query(
            parse_create_dictionary(ddl.replace("CLICKHOUSE", "MYSQL")), "analytics", "events"
        )
        is None
    )


def test_inspection_includes_source_mv_inner_engine_and_target_mv(monkeypatch):
    schema = SimpleNamespace(
        views={"v": SimpleNamespace(select_query="SELECT id FROM analytics.events")},
        materialized_views={
            "inner": SimpleNamespace(select_query="SELECT id FROM events", target_table=None),
            "writer": SimpleNamespace(
                select_query="SELECT id FROM other.source", target_table="events"
            ),
            "elsewhere": SimpleNamespace(
                select_query="SELECT id FROM other.events", target_table="other.events"
            ),
        },
        dictionaries={},
    )
    monkeypatch.setattr("ch_migrate.rebuild_dependents.get_live_schema", lambda *_: schema)
    client = _Client()
    inventory = inspect_dependents(client, "analytics", "events")
    assert [d.name for d in inventory.source_views] == ["v", "inner"]
    assert [d.name for d in inventory.target_views] == ["writer"]
    assert not inventory.dictionaries


def test_validation_refuses_missing_source_projection_with_named_error():
    client = _Client({"source": {"id": "UInt64"}})
    definition = SimpleNamespace(database="analytics", table="events")
    inventory = DependentInventory(
        (DependentQuery("broken", "SELECT obsolete FROM events", "materialized_view"),), (), ()
    )

    class RejectMissing(_Client):
        def query(self, sql, parameters=None):
            if sql.startswith("EXPLAIN"):
                raise RuntimeError("Unknown identifier obsolete")
            return super().query(sql, parameters)

    with pytest.raises(
        DependentValidationError, match="materialized_view analytics.broken.*obsolete"
    ):
        validate_dependents(RejectMissing(), definition, inventory)


def test_target_view_names_a_server_rejected_conversion():
    class RejectCast(_Client):
        def query(self, sql, parameters=None):
            if sql.startswith("EXPLAIN PLAN SELECT CAST("):
                raise RuntimeError("Cannot convert Array(UInt64) to UInt64")
            return super().query(sql, parameters)

    client = RejectCast({"source": {"id": "Array(UInt64)"}, "target": {"id": "UInt64"}})
    definition = SimpleNamespace(database="analytics", table="events")
    inventory = DependentInventory(
        (),
        (DependentQuery("writer", "SELECT [id] AS id FROM other.source", "materialized_view"),),
        (),
    )
    with pytest.raises(DependentValidationError, match="writer.*Cannot convert Array"):
        validate_dependents(client, definition, inventory)


def test_dictionary_projection_rejects_missing_attribute():
    ddl = (
        "CREATE DICTIONARY analytics.lookup (id UInt64, label String) PRIMARY KEY id "
        "SOURCE(CLICKHOUSE(DB 'analytics' TABLE 'events')) LAYOUT(HASHED())"
    )

    class DictionaryClient(_Client):
        def query(self, sql, parameters=None):
            if sql.startswith("SHOW CREATE"):
                return _Result(result_rows=((ddl,),))
            return super().query(sql, parameters)

    client = DictionaryClient({"source": {"id": "UInt64"}})
    inventory = DependentInventory(
        (), (), (DependentQuery("lookup", "SELECT id, label FROM events", "dictionary"),)
    )
    with pytest.raises(DependentValidationError, match="lookup.*label"):
        validate_dependents(
            client, SimpleNamespace(database="analytics", table="events"), inventory
        )


def test_reload_uses_effective_cluster_and_names_failure():
    class FailingClient(_Client):
        def command(self, sql):
            super().command(sql)
            if sql.endswith("`lookup2`"):
                raise RuntimeError("reload failed")

    client = FailingClient()
    with pytest.raises(DependentValidationError, match="analytics.lookup2.*reload failed"):
        reload_dictionaries(
            client, DictionaryReload("analytics", ("lookup", "lookup2"), "generated")
        )
