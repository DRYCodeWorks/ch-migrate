"""Tests for package metadata."""

from importlib.metadata import version

import clickhouse_alembic


def test_version_matches_distribution_metadata():
    # __version__ is the single source hatch builds from; the installed
    # distribution must report the same number.
    assert clickhouse_alembic.__version__ == version("clickhouse-alembic")
