"""Engine policy boundaries; Cloud behavior itself still requires live acceptance."""

import pytest

from ch_migrate.version_table import VersionTableState


@pytest.mark.parametrize(
    "database_engine,cluster,engine,on_cluster",
    [
        ("Shared", "ignored_on_cloud", "MergeTree", None),
        ("Replicated", "ignored_for_database_ddl", "ReplicatedMergeTree", None),
        ("Atomic", "it_cluster", "ReplicatedMergeTree", "it_cluster"),
        ("Atomic", None, "MergeTree", None),
    ],
)
def test_version_table_engine_selection(database_engine, cluster, engine, on_cluster):
    state = VersionTableState("example", database_engine, cluster)
    assert state.new_engine().name == engine
    assert state.on_cluster == on_cluster


def test_version_table_keeper_path_encodes_database_without_encoding_macros():
    state = VersionTableState("example/quoted'name", "Atomic", "it_cluster")
    ddl = state.new_engine().compile()
    assert "example%2Fquoted%27name" in ddl
    assert "{shard}" in ddl and "{replica}" in ddl


def test_version_table_warns_only_for_existing_unreplicated_deployment():
    state = VersionTableState("example", "Atomic", "it_cluster", "ReplacingMergeTree")
    warning = state.warning()
    assert "non-replicated ReplacingMergeTree" in warning
    assert "reconcile" in warning and "never converted automatically" in warning
    assert VersionTableState("example", "Atomic", None, "ReplacingMergeTree").warning() is None
    assert VersionTableState("example", "Replicated", None, "ReplicatedMergeTree").warning() is None
    assert VersionTableState("example", "Shared", None, "SharedMergeTree").warning() is None
    assert VersionTableState("example", "Atomic", "it_cluster").warning() is None
