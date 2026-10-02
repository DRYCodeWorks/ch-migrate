"""Follow the README on a fixture-owned server, including bootstrap and .env.local."""

import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration


@pytest.fixture
def quickstart_project(clickhouse_server, tmp_path, request, monkeypatch):
    for suffix in ("PASSWORD", "ADMIN_PASSWORD", "MIGRATION_PASSWORD"):
        monkeypatch.delenv(f"CH_DEV_{suffix}", raising=False)
    _cli(tmp_path, "init", "--name", "my_project")
    client, database = _configure(tmp_path, clickhouse_server, request)
    return tmp_path, client, database


def test_readme_quickstart(quickstart_project):
    root, client, database = quickstart_project
    _cli(root, "bootstrap", "dev", "--dry-run")
    _cli(root, "bootstrap", "dev")
    _cli(root, "new", "dev", "add_status", "--table", "logs")
    upgrade_sql, downgrade_sql = re.findall(r"```sql\n(.*?)```", _quick_start(), re.DOTALL)
    [upgrade] = list((root / "migrations" / "sql").rglob("*.up.sql"))
    [downgrade] = list((root / "migrations" / "sql").rglob("*.down.sql"))
    upgrade.write_text(upgrade_sql)
    downgrade.write_text(downgrade_sql)
    _cli(root, "up", "dev")
    status = _cli(root, "status", "dev")
    assert re.search(r"Applied:\s+1\b", status), status
    _cli(root, "history", "dev")
    assert client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} "
        "AND table = 'logs' ORDER BY position",
        parameters={"db": database},
    ).result_rows == [("id",), ("status",)]
    _cli(root, "down", "dev")
    assert client.command(f"EXISTS TABLE {database}.logs") == 0


def _configure(root, server, request):
    config = yaml.safe_load(re.search(r"```yaml\n(.*?)```", _quick_start(), re.DOTALL).group(1))
    database = "it_" + secrets.token_hex(8)
    user = database + "_migration"
    config["project"]["name"] = database
    config["defaults"].update(port=server.port, secure=server.secure, admin_user=server.user)
    config["environments"]["dev"].update(host=server.host, database=database, migration_user=user)
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    (root / ".env.local").write_text(
        f"CH_DEV_ADMIN_PASSWORD={server.password}\n"
        f"CH_DEV_MIGRATION_PASSWORD={secrets.token_hex(24)}\n"
    )
    client = server.connect()
    request.addfinalizer(client.close)
    request.addfinalizer(lambda: client.command(f"DROP ROLE IF EXISTS {database}_migration_role"))
    request.addfinalizer(lambda: client.command(f"DROP USER IF EXISTS {user}"))
    request.addfinalizer(lambda: client.command(f"DROP DATABASE IF EXISTS {database} SYNC"))
    return client, database


def _cli(root, *args):
    result = subprocess.run(
        [sys.executable, "-m", "clickhouse_alembic.cli", *args],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _quick_start():
    readme = (Path(__file__).parents[2] / "README.md").read_text()
    return re.search(r"^## Quick start\n(.*?)(?=^## )", readme, re.MULTILINE | re.DOTALL).group(1)
