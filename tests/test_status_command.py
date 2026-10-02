"""`ch-migrate status` is a report: CI runs it as a non-blocking check.

Sazabi's required "ClickHouse Migration Status" job relies on status exiting 0 when
the database is unreachable or migrations are pending; only a broken project fails.
"""

import pytest
from click.testing import CliRunner

from ch_migrate import connection
from ch_migrate.cli import main


@pytest.fixture
def project(tmp_path, monkeypatch):
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "demo"])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CH_DEV_MIGRATION_PASSWORD", "test-only")
    return tmp_path


def test_unreachable_database_is_reported_and_exits_zero(project, monkeypatch):
    def refuse(env_config):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(connection, "get_migration_state", refuse)
    result = CliRunner().invoke(main, ["status", "dev"])
    assert result.exit_code == 0, result.output
    assert "Could not reach the database: Connection refused" in result.output


def test_unknown_environment_fails(project):
    result = CliRunner().invoke(main, ["status", "nope"])
    assert result.exit_code == 1
    assert "Unknown environment: nope" in result.output
