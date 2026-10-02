"""Opt-in real-server tests; each session owns only its chm-it-* container."""

from __future__ import annotations

import configparser
import os
import secrets
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

import clickhouse_connect
import pytest
import yaml
from click.testing import CliRunner

from clickhouse_alembic.cli import main


@dataclass(frozen=True)
class ClickHouseServer:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    secure: bool = False

    def connect(self):
        return clickhouse_connect.get_client(
            host=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            secure=self.secure,
        )


@dataclass(frozen=True)
class MigrationProject:
    root: Path
    client: object = field(repr=False)
    database: str

    @property
    def sql_dir(self):
        return self.root / "migrations" / "sql"

    @property
    def versions_dir(self):
        return self.root / "migrations" / "versions"

    def run(self, *args):
        return CliRunner().invoke(main, list(args), catch_exceptions=True)

    def write_revision(self, revision, bodies, down_revision=None):
        """Write upgrade/downgrade bodies without depending on `new`."""
        path = self.versions_dir / f"{revision}.py"
        source = (
            "from alembic import op\n"
            "from clickhouse_alembic import get_db\n\n"
            f"revision = {revision!r}\n"
            f"down_revision = {down_revision!r}\n"
            "branch_labels = None\ndepends_on = None\n"
        )
        for direction in ("upgrade", "downgrade"):
            body = textwrap.dedent(bodies.get(direction, "pass")).strip()
            source += f"\ndef {direction}():\n    db = get_db()\n"
            source += textwrap.indent(body, "    ") + "\n"
        path.write_text(source)
        return path


@pytest.fixture(scope="session")
def clickhouse_server(request):
    url = os.environ.get("CH_MIGRATE_IT_URL")
    if url:
        return _server_from_url(url)
    if not shutil.which("docker"):
        pytest.skip("Docker is unavailable and CH_MIGRATE_IT_URL is not set")
    available = subprocess.run(["docker", "info"], capture_output=True)
    if available.returncode:
        pytest.skip("Docker daemon is unavailable and CH_MIGRATE_IT_URL is not set")
    name = "chm-it-" + secrets.token_hex(8)
    password = secrets.token_hex(24)
    request.addfinalizer(lambda: _remove_container(name))
    _start_container(name, password)
    result = subprocess.run(
        ["docker", "port", name, "8123/tcp"], check=True, capture_output=True, text=True
    )
    port = int(result.stdout.strip().rsplit(":", 1)[1])
    server = ClickHouseServer("127.0.0.1", port, "default", password)
    _wait_for_ping(server)
    return server


@pytest.fixture
# Pytest injects independent fixture dependencies through the public signature.
def project(clickhouse_server, tmp_path, monkeypatch, request):
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "integration"])
    assert result.exit_code == 0, result.output
    _configure_project(tmp_path, clickhouse_server)
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", clickhouse_server.password)
    monkeypatch.chdir(tmp_path)
    client = clickhouse_server.connect()
    request.addfinalizer(client.close)
    database = "it_" + secrets.token_hex(8)
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    config["environments"]["it"]["database"] = database
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    request.addfinalizer(lambda: client.command(f"DROP DATABASE IF EXISTS {database} SYNC"))
    client.command(f"CREATE DATABASE {database}")
    return MigrationProject(tmp_path, client, database)


def _server_from_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        pytest.fail("CH_MIGRATE_IT_URL must be an HTTP(S) server URL", pytrace=False)
    return ClickHouseServer(
        parsed.hostname,
        parsed.port or (8443 if parsed.scheme == "https" else 8123),
        unquote(parsed.username or "default"),
        unquote(parsed.password or ""),
        parsed.scheme == "https",
    )


def _start_container(name, password):
    image = os.environ.get("CH_MIGRATE_IT_IMAGE", "26.3")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8123",
            "--env",
            "CLICKHOUSE_PASSWORD",
            "--env",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            f"clickhouse/clickhouse-server:{image}",
        ],
        env={**os.environ, "CLICKHOUSE_PASSWORD": password},
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.fail(f"Could not start integration container: {result.stderr}", pytrace=False)


def _remove_container(name):
    result = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True)
    if result.returncode and "No such container" not in result.stderr:
        pytest.fail(f"Could not remove integration container {name}", pytrace=False)


def _wait_for_ping(server):
    deadline = time.monotonic() + 60
    url = f"http://{server.host}:{server.port}/ping"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.read().strip() == b"Ok.":
                    return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    pytest.fail("Integration container did not become ready within 60 seconds", pytrace=False)


def _configure_project(root, server):
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(root / "alembic.ini")
    ini.remove_section("post_write_hooks")
    with (root / "alembic.ini").open("w") as stream:
        ini.write(stream)
    config = {
        "project": {"name": "integration"},
        "environments": {
            "it": {
                "host": server.host,
                "port": server.port,
                "secure": server.secure,
                "migration_user": server.user,
            }
        },
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config))
