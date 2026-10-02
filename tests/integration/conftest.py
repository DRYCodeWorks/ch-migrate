"""Opt-in real-server tests; each session owns only its chm-it-* container."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

import clickhouse_connect
import pytest
import yaml
from click.testing import CliRunner

from ch_migrate.cli import main


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
            connect_timeout=5,
            send_receive_timeout=15,
        )


@dataclass
class ClickHouseCluster:
    """One owned shard with two replicas; Keeper runs inside node 1."""

    network: str
    containers: dict[int, str]
    hostnames: dict[int, str]
    servers: dict[int, ClickHouseServer]
    clients: dict[int, Any] = field(default_factory=dict, repr=False)
    stopped: set[int] = field(default_factory=set)
    name: str = "it_cluster"

    def stop_node(self, node: int) -> None:
        _docker("stop", "--time", "0", self.containers[node])
        self.stopped.add(node)

    def start_node(self, node: int) -> None:
        _docker("start", self.containers[node])
        self.servers[node] = replace(
            self.servers[node], port=_container_port(self.containers[node])
        )
        _wait_for_ping(self.servers[node])
        self.clients[node].close()
        self.clients[node] = self.servers[node].connect()
        self.stopped.discard(node)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()


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
            "from ch_migrate import get_db\n\n"
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
        server = _server_from_url(url)
        _wait_for_query(server)
        return server
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


@pytest.fixture(scope="session")
def clickhouse_cluster(request, tmp_path_factory):
    if os.environ.get("CH_MIGRATE_IT_URL"):
        pytest.skip("Cluster tests only use their own Docker nodes, never an external server")
    if (
        not shutil.which("docker")
        or subprocess.run(["docker", "info"], capture_output=True).returncode
    ):
        pytest.skip("Docker is unavailable for the replicated integration fixture")
    prefix = "chm-it-" + secrets.token_hex(8)
    network = _docker("network", "create", prefix)
    request.addfinalizer(lambda: _remove_network(network))
    hostnames = {node: f"{prefix}-node{node}" for node in (1, 2)}
    root = tmp_path_factory.mktemp("cluster-config")
    password = secrets.token_hex(24)
    containers = {}
    # Keep acquisition and LIFO finalizers together: only successfully created IDs are removed.
    for node, name in hostnames.items():
        config_path = root / f"node{node}.xml"
        config_path.write_text(_cluster_xml(hostnames, node))
        container = _create_cluster_container(name, password, (prefix, config_path))
        containers[node] = container
        request.addfinalizer(lambda owned=container: _remove_container(owned))
        _docker("start", container)
    servers = {
        node: ClickHouseServer("127.0.0.1", _container_port(container), "default", password)
        for node, container in containers.items()
    }
    cluster = ClickHouseCluster(prefix, containers, hostnames, servers)
    request.addfinalizer(cluster.close)
    for node, server in servers.items():
        _wait_for_ping(server)
        cluster.clients[node] = server.connect()
    _wait_for_cluster(cluster)
    return cluster


@pytest.fixture
def cluster_project(clickhouse_cluster, tmp_path, monkeypatch, request):
    cluster = clickhouse_cluster
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "integration"])
    assert result.exit_code == 0, result.output
    _configure_project(tmp_path, cluster.servers[1])
    database = "it_" + secrets.token_hex(8)
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    config["environments"]["it"].update(database=database, cluster=cluster.name)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", cluster.servers[1].password)
    monkeypatch.chdir(tmp_path)
    request.addfinalizer(lambda: _drop_cluster_database(cluster, database))
    cluster.clients[1].command(
        f"CREATE DATABASE {database} ON CLUSTER {cluster.name} ENGINE = Atomic"
    )
    return MigrationProject(tmp_path, cluster.clients[1], database)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        if "clickhouse_cluster" in item.fixturenames:
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.cluster)


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
    result = subprocess.run(
        ["docker", "rm", "--force", "--volumes", name], capture_output=True, text=True
    )
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


def _wait_for_query(server):
    """Wait for an external server; an idle-scaled ClickHouse Cloud service needs time to wake."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            client = server.connect()
            client.command("SELECT 1")
            client.close()
            return
        except Exception:  # noqa: BLE001 - any failure before the deadline means "not awake yet"
            time.sleep(2)
    pytest.fail("CH_MIGRATE_IT_URL server did not answer within 300 seconds", pytrace=False)


def _configure_project(root, server):
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


def _docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _container_port(container):
    return int(_docker("port", container, "8123/tcp").rsplit(":", 1)[1])


def _create_cluster_container(name, password, resources):
    network, config_path = resources
    image = os.environ.get("CH_MIGRATE_IT_IMAGE", "26.3")
    result = subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--hostname",
            name,
            "--network",
            network,
            "--publish",
            "127.0.0.1::8123",
            "--env",
            "CLICKHOUSE_PASSWORD",
            "--env",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "--volume",
            f"{config_path}:/etc/clickhouse-server/config.d/integration.xml:ro",
            f"clickhouse/clickhouse-server:{image}",
        ],
        env={**os.environ, "CLICKHOUSE_PASSWORD": password},
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.fail(f"Could not create cluster node: {result.stderr}", pytrace=False)
    return result.stdout.strip()


def _remove_network(network):
    result = subprocess.run(["docker", "network", "rm", network], capture_output=True, text=True)
    if result.returncode and "not found" not in result.stderr:
        pytest.fail("Could not remove the owned integration network", pytrace=False)


def _drop_cluster_database(cluster, database):
    for node in sorted(cluster.stopped):
        cluster.start_node(node)
    cluster.clients[1].command(f"DROP DATABASE IF EXISTS {database} ON CLUSTER {cluster.name} SYNC")


def _wait_for_cluster(cluster):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            count = cluster.clients[1].command(
                f"SELECT count() FROM clusterAllReplicas('{cluster.name}', system.one)",
                settings={"max_execution_time": 5, "connect_timeout_with_failover_ms": 200},
            )
            cluster.clients[1].command("SELECT count() FROM system.zookeeper WHERE path = '/'")
            if count == 2:
                return
        except Exception:
            pass
        time.sleep(0.2)
    pytest.fail("Owned replicas or Keeper did not become ready within 60 seconds", pytrace=False)


def _cluster_xml(hostnames, node):
    replicas = "".join(
        f"<replica><host>{name}</host><port>9000</port><user>default</user>"
        '<password from_env="CLICKHOUSE_PASSWORD"/></replica>'
        for name in hostnames.values()
    )
    keeper = _keeper_xml(hostnames[1]) if node == 1 else ""
    return f"""<clickhouse>
  <listen_host replace="replace">0.0.0.0</listen_host>
  <interserver_http_host>{hostnames[node]}</interserver_http_host>
  {keeper}
  <zookeeper><node><host>{hostnames[1]}</host><port>9181</port></node></zookeeper>
  <named_collections><chm_it_auth><cluster_username>default</cluster_username>
    <cluster_password from_env="CLICKHOUSE_PASSWORD"/>
  </chm_it_auth></named_collections>
  <database_replicated><collection_name>chm_it_auth</collection_name></database_replicated>
  <remote_servers>
    <it_cluster><shard><internal_replication>true</internal_replication>{replicas}</shard></it_cluster>
  </remote_servers>
  <macros><shard>01</shard><replica>r{node}</replica></macros>
  <distributed_ddl><path>/clickhouse/task_queue/ddl</path></distributed_ddl>
</clickhouse>
"""


def _keeper_xml(hostname):
    return f"""<keeper_server>
    <tcp_port>9181</tcp_port>
    <server_id>1</server_id>
    <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
    <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
    <raft_configuration>
      <server><id>1</id><hostname>{hostname}</hostname><port>9234</port></server>
    </raft_configuration>
  </keeper_server>"""
