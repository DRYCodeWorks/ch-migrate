"""Explicit DRY-1391 reproduction; not part of automatic test collection.

Run this file with CH_MIGRATE_SPIKE_PYTHON pointing to the clean 3.10 environment
containing clickhouse-connect[alembic]==1.9.0 and clickhouse-sqlalchemy==0.3.2.
The normal integration fixture owns the server and all database cleanup.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

pytestmark = pytest.mark.integration

SPIKE = Path(__file__).parents[2] / "docs/design/spikes/2026-10-02-dialect"


@pytest.mark.parametrize("mode", ["official", "legacy", "legacy_url", "legacy_connect_args"])
def test_dialect_session_reproduction(project, clickhouse_server, monkeypatch, mode):
    directory = "official" if mode == "official" else "legacy"
    shutil.copyfile(SPIKE / directory / "env.py", project.root / "migrations/env.py")
    for name in ("r001_set_and_probe.py", "r002_next_revision.py"):
        shutil.copyfile(SPIKE / "versions" / name, project.versions_dir / name)
    monkeypatch.setenv("CH_ENVIRONMENT", "it")
    monkeypatch.setenv("CH_DATABASE", project.database)
    monkeypatch.setenv("SPIKE_URL_QUERY", "")
    monkeypatch.setenv("SPIKE_CONNECT_ARGS", "{}")
    _configure_mode(mode, (project, clickhouse_server), monkeypatch)
    baseline = str(project.client.command("SELECT getSetting('max_threads')"))
    assert baseline != "1", "The fixture server must distinguish the default from SET 1"
    result = subprocess.run(
        [os.environ["CH_MIGRATE_SPIKE_PYTHON"], "-m", "alembic", "upgrade", "head"],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = project.client.query(
        f"SELECT rev, val FROM {project.database}.spike_probe ORDER BY rev"
    ).result_rows
    expected = "1" if mode in ("official", "legacy_connect_args") else baseline
    assert rows == [("r001", expected), ("r002", expected)]
    print(
        f"{mode}: server_default={baseline}; same_revision={rows[0][1]}; next_revision={rows[1][1]}"
    )


def _configure_mode(mode, connection, monkeypatch):
    project, server = connection
    if mode == "official":
        url = (
            f"clickhousedb://{quote(server.user, safe='')}:{quote(server.password, safe='')}"
            f"@{server.host}:{server.port}/{project.database}"
        )
        monkeypatch.setenv("SPIKE_URL", url)
    elif mode == "legacy_url":
        monkeypatch.setenv("SPIKE_URL_QUERY", f"?session_id={project.database}")
    elif mode == "legacy_connect_args":
        monkeypatch.setenv(
            "SPIKE_CONNECT_ARGS", json.dumps({"ch_settings": {"session_id": project.database}})
        )
