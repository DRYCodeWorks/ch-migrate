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
from access_fixtures import access_project, bootstrapped_project, limited_admin  # noqa: F401
from click.testing import CliRunner

from ch_migrate.cli import main

_CLUSTER_FIXTURES = {"clickhouse_cluster", "clickhouse_cluster_keeper"}
# An image older than the 25.8 the live access commands need (design note, Decision 11).
OLD_IMAGE = "24.8"
# "local" keeps the image's default `user_directories`. "replicated" replaces it so that users
# and roles created by SQL go to Keeper, where every node reads them.
_ACCESS_STORAGE_XML = {
    "local": "",
    "replicated": """<user_directories replace="replace">
    <users_xml><path>users.xml</path></users_xml>
    <replicated><zookeeper_path>/clickhouse/access/</zookeeper_path></replicated>
  </user_directories>""",
}


# CLUSTER for every login, as on ClickHouse Cloud: ON CLUSTER then needs no grant.
_IMPLICIT_CLUSTER_XML = """<clickhouse>
  <access_control_improvements>
    <on_cluster_queries_require_cluster_grant>false</on_cluster_queries_require_cluster_grant>
  </access_control_improvements>
</clickhouse>
"""
# A read-only directory before the one a create goes to, as Cloud lists `jwt` first.
_LDAP_FIRST_XML = """<clickhouse>
  <ldap_servers>
    <chm_it_ldap><host>127.0.0.1</host><port>1389</port><enable_tls>no</enable_tls>
      <bind_dn>uid={user_name},dc=example,dc=org</bind_dn></chm_it_ldap>
  </ldap_servers>
  <user_directories replace="replace">
    <users_xml><path>users.xml</path></users_xml>
    <ldap><server>chm_it_ldap</server></ldap>
    <local_directory><path>/var/lib/clickhouse/access/</path></local_directory>
  </user_directories>
</clickhouse>
"""


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
            # Cloud DDL such as DROP DATABASE SYNC can take longer than a local container.
            send_receive_timeout=60 if self.secure else 15,
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
    access_storage: str = "local"  # where users and roles live: "local" or "replicated"

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
    cloud: bool = False

    # SharedMergeTree lists metadata-only ALTERs in system.mutations with no block numbers.
    # They rewrite no parts, so tests count only part-rewriting mutations.
    # Needs {db:String} and {table:String} parameters.
    REWRITES_PARTS = (
        "NOT (empty(block_numbers.number) AND startsWith((SELECT engine FROM system.tables "
        "WHERE database = {db:String} AND name = {table:String}), 'Shared'))"
    )

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

    def stop_merges(self, table):
        """Hold mutations pending. ClickHouse Cloud grants this to no user, so such tests skip."""
        if self.cloud:
            pytest.skip("ClickHouse Cloud cannot grant SYSTEM STOP MERGES to hold a mutation")
        self.client.command(f"SYSTEM STOP MERGES {table}")

    def engine(self, name):
        """The engine name the server reports: Cloud's Shared databases use Shared* engines."""
        return "Shared" + name if self.cloud else name

    def query_log(self):
        """Every replica's query log; on Cloud each node keeps only the queries it served."""
        if self.cloud:
            return "clusterAllReplicas('default', merge('system', '^query_log'))"
        return "system.query_log"

    def everywhere(self, table):
        """FROM source for a per-node table (Memory, most system tables) on every Cloud replica."""
        if self.cloud:
            return f"clusterAllReplicas('default', {table})"
        return table

    def flush_logs(self):
        """Flush query logs on every replica; Cloud keeps them per node."""
        on = " ON CLUSTER 'default'" if self.cloud else ""
        self.client.command(f"SYSTEM FLUSH LOGS{on}")

    def drain_async_queue(self, table):
        """Flush `table`'s async queue; Cloud forbids the flush, so wait for it to empty."""
        if not self.cloud:
            self.client.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE {self.database}.{table}")
            return
        deadline = time.monotonic() + 60
        while self.client.command(
            f"SELECT count() FROM {self.everywhere('system.asynchronous_inserts')} "
            "WHERE database = {db:String} AND table = {table:String}",
            parameters={"db": self.database, "table": table},
        ):
            if time.monotonic() > deadline:
                pytest.fail(f"Async insert queue for {table} did not drain within 60 seconds")
            time.sleep(0.2)

    def budget(self, seconds):
        """An `up --timeout` value; Cloud's all-replica checks and grace need at least a minute."""
        return str(max(seconds, 60) if self.cloud else seconds)

    @staticmethod
    def new_password():
        # Cloud's password policy needs an uppercase letter and a special character.
        return "Chm!" + secrets.token_hex(24)


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
def old_clickhouse_server(request):
    """ClickHouse 24.8, older than the live access commands' 25.8 floor, in its own container."""
    if os.environ.get("CH_MIGRATE_IT_URL"):
        pytest.skip("The 24.8 checks only use their own Docker container, never an external server")
    if (
        not shutil.which("docker")
        or subprocess.run(["docker", "info"], capture_output=True).returncode
    ):
        pytest.skip("Docker is unavailable for the ClickHouse 24.8 container")
    name = "chm-it-" + secrets.token_hex(8)
    password = secrets.token_hex(24)
    request.addfinalizer(lambda: _remove_container(name))
    _start_container(name, password, _Image(OLD_IMAGE))
    server = ClickHouseServer("127.0.0.1", _container_port(name), "default", password)
    _wait_for_ping(server)
    return server


@pytest.fixture(scope="session")
def implicit_cluster_server(request, tmp_path_factory):
    """A server that grants CLUSTER to every login, so ON CLUSTER needs no grant.

    ClickHouse Cloud does: there it is the Cloud service itself. Elsewhere it is a container
    of its own with `on_cluster_queries_require_cluster_grant` off.
    """
    return _cloud_or_configured(request, tmp_path_factory, _IMPLICIT_CLUSTER_XML)


@pytest.fixture(scope="session")
def read_only_directory_server(request, tmp_path_factory):
    """A server whose first user directory after users.xml can't take a create.

    ClickHouse Cloud lists `jwt` before `replicated`: there it is the Cloud service itself.
    Elsewhere it is a container of its own that lists `ldap` before `local_directory`. Its
    LDAP server doesn't exist, so a user created by SQL can't log in there: use users.xml's
    `default`, the admin.
    """
    return _cloud_or_configured(request, tmp_path_factory, _LDAP_FIRST_XML)


@pytest.fixture(scope="session")
def server_is_cloud(clickhouse_server):
    """ClickHouse Cloud services create Shared databases; self-hosted servers cannot."""
    client = clickhouse_server.connect()
    try:
        return bool(client.command("SELECT count() FROM system.databases WHERE engine = 'Shared'"))
    finally:
        client.close()


@pytest.fixture(autouse=True)
def cloud_scope(request):
    """Run `cloud`-marked tests only against a ClickHouse Cloud CH_MIGRATE_IT_URL."""
    if request.node.get_closest_marker("cloud") is None:
        return
    if not os.environ.get("CH_MIGRATE_IT_URL") or not request.getfixturevalue("server_is_cloud"):
        pytest.skip("ClickHouse Cloud-only check; CH_MIGRATE_IT_URL is not a Cloud service")


@pytest.fixture(scope="session")
def clickhouse_cluster(request, tmp_path_factory):
    """Users and roles live on each node's local disk, as in the server's default config."""
    return _start_cluster(request, tmp_path_factory, "local")


@pytest.fixture(scope="session")
def clickhouse_cluster_keeper(request, tmp_path_factory):
    """The same cluster, with users and roles stored in Keeper and shared by both nodes."""
    return _start_cluster(request, tmp_path_factory, "replicated")


@pytest.fixture
def cluster_project(clickhouse_cluster, tmp_path, monkeypatch, request):
    cluster = clickhouse_cluster
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "integration"])
    assert result.exit_code == 0, result.output
    (tmp_path / "access.yaml").unlink()  # legacy mode, as these tests were written for
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
        if _CLUSTER_FIXTURES & set(item.fixturenames):
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.cluster)


@pytest.fixture
# Pytest injects independent fixture dependencies through the public signature.
def project(clickhouse_server, tmp_path, monkeypatch, request):
    result = CliRunner().invoke(main, ["init", str(tmp_path), "--name", "integration"])
    assert result.exit_code == 0, result.output
    (tmp_path / "access.yaml").unlink()  # legacy mode, as these tests were written for
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
    return MigrationProject(
        tmp_path, client, database, cloud=request.getfixturevalue("server_is_cloud")
    )


@dataclass(frozen=True)
class _Image:
    """The image a container runs, and a file for its config.d, if any."""

    tag: str | None = None  # None: CH_MIGRATE_IT_IMAGE, else 26.3
    config: Path | None = None


@dataclass(frozen=True)
class _ClusterSpec:
    prefix: str
    hostnames: dict[int, str]
    root: Path
    password: str = field(repr=False)
    access_storage: str


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


def _cloud_or_configured(request, tmp_path_factory, xml):
    """The Cloud service, or else a container of its own with `xml` in its config.d."""
    if os.environ.get("CH_MIGRATE_IT_URL"):
        if not request.getfixturevalue("server_is_cloud"):
            pytest.skip("This server setup needs ClickHouse Cloud or a Docker container")
        return request.getfixturevalue("clickhouse_server")
    if not shutil.which("docker"):
        pytest.skip("Docker is unavailable and CH_MIGRATE_IT_URL is not set")
    config = tmp_path_factory.mktemp("server-config") / "chm_it.xml"
    config.write_text(xml)
    name = "chm-it-" + secrets.token_hex(8)
    password = secrets.token_hex(24)
    request.addfinalizer(lambda: _remove_container(name))
    _start_container(name, password, _Image(config=config))
    server = ClickHouseServer("127.0.0.1", _container_port(name), "default", password)
    _wait_for_ping(server)
    return server


def _start_container(name, password, image=_Image()):
    tag = image.tag or os.environ.get("CH_MIGRATE_IT_IMAGE", "26.3")
    mount = ["--volume", f"{image.config}:/etc/clickhouse-server/config.d/chm_it.xml:ro"]
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
            *(mount if image.config else []),
            f"clickhouse/clickhouse-server:{tag}",
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


def _start_cluster(request, tmp_path_factory, access_storage):
    """Two Docker nodes on their own network; `access_storage` is "local" or "replicated"."""
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
    spec = _ClusterSpec(prefix, hostnames, root, secrets.token_hex(24), access_storage)
    containers = _start_cluster_nodes(request, spec)
    servers = {
        node: ClickHouseServer("127.0.0.1", _container_port(container), "default", spec.password)
        for node, container in containers.items()
    }
    cluster = ClickHouseCluster(prefix, containers, hostnames, servers)
    cluster.access_storage = access_storage
    request.addfinalizer(cluster.close)
    for node, server in servers.items():
        _wait_for_ping(server)
        cluster.clients[node] = server.connect()
    _wait_for_cluster(cluster)
    return cluster


def _start_cluster_nodes(request, spec):
    containers = {}
    # Keep acquisition and LIFO finalizers together: only successfully created IDs are removed.
    for node, name in spec.hostnames.items():
        config_path = spec.root / f"node{node}.xml"
        config_path.write_text(_cluster_xml(spec, node))
        container = _create_cluster_container(name, spec.password, (spec.prefix, config_path))
        containers[node] = container
        request.addfinalizer(lambda owned=container: _remove_container(owned))
        _docker("start", container)
        if node == 1 and spec.access_storage == "replicated":
            # Node 2 reads its users from node 1's Keeper at startup and exits without it.
            port = _container_port(container)
            _wait_for_ping(ClickHouseServer("127.0.0.1", port, "default", spec.password))
    return containers


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


def _cluster_xml(spec, node):
    hostnames = spec.hostnames
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
  {_ACCESS_STORAGE_XML[spec.access_storage]}
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
