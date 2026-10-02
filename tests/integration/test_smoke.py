"""Exercise the generated project and CLI against an actual ClickHouse server."""

import re

import pytest

pytestmark = pytest.mark.integration


def test_up_status_down(project):
    project.write_revision(
        "smoke_001",
        {
            "upgrade": 'op.execute(f"CREATE TABLE IF NOT EXISTS {db}.smoke '
            '(id UInt64) ENGINE = MergeTree ORDER BY id")',
            "downgrade": 'op.execute(f"DROP TABLE IF EXISTS {db}.smoke")',
        },
    )
    upgraded = project.run("up", "it")
    assert upgraded.exit_code == 0, upgraded.output
    assert project.client.command(f"EXISTS TABLE {project.database}.smoke") == 1
    assert project.client.query(
        f"SELECT version_num FROM {project.database}.alembic_version"
    ).result_rows == [("smoke_001",)]
    status = project.run("status", "it")
    assert status.exit_code == 0, status.output
    assert re.search(r"Applied:\s+1\b", status.output), status.output
    assert re.search(r"Pending:\s+0\b", status.output), status.output
    downgraded = project.run("down", "it")
    assert downgraded.exit_code == 0, downgraded.output
    assert project.client.command(f"EXISTS TABLE {project.database}.smoke") == 0
