"""The live reader without a server: one read, reads after writes, and the version floor.

`FakeServer` answers the statements in `sql/live.sql` from tables of rows.
"""

import time

import pytest
import yaml
from fake_access_server import FakeServer, grant_rows

from ch_migrate.access.grants import parse_grant
from ch_migrate.access.live import (
    LiveState,
    Membership,
    Patience,
    Wanted,
    read_live,
    read_settled,
)
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.rights import Right, Scope
from ch_migrate.access.server import (
    AccessError,
    Login,
    connect,
    connect_admin,
    require_supported_version,
)
from ch_migrate.access.settings import ProjectSettings, ServerSettings, SettingsError, load_settings

TREE = bundled_tree()


def held_by(holder, text, **flags):
    """`system.grants` rows for a grant held by `holder`, such as "role analyst"."""
    return [{**_holder(holder), **row} for row in grant_rows(parse_grant(text), **flags)]


def membership(holder, role, **flags):
    row = {**_holder(holder), "granted_role_name": role}
    return {**row, "granted_role_is_default": 1, "with_admin_option": 0, **flags}


def _holder(holder):
    kind, name = holder.split()
    return {"user_name": None, "role_name": None, f"{kind}_name": name}


@pytest.fixture
def live():
    """One read of a server with a role, two users it was asked about, and one it wasn't."""
    server = FakeServer(storage="local_directory")
    server.tables["roles"] = [{"name": "analyst"}]
    server.tables["users"] = [
        {"name": "etl", "default_roles_all": 0, "default_roles_except": []},
        {"name": "analyst", "default_roles_all": 1, "default_roles_except": ["loader"]},
        {"name": "stranger", "default_roles_all": 1, "default_roles_except": []},
    ]
    server.tables["grants"] = [
        *held_by("role analyst", "SELECT ON d.* WITH GRANT OPTION"),
        *held_by("role analyst", "SELECT ON d.secrets", is_partial_revoke=1),
        *held_by("user etl", "READ ON REMOTE"),
        *held_by("user stranger", "INSERT ON d.*"),
    ]
    server.tables["role_grants"] = [
        membership("user etl", "loader", granted_role_is_default=0),
        membership("user etl", "analyst", with_admin_option=1),
        membership("user stranger", "analyst"),
    ]
    state = read_live(server, Wanted(("analyst", "etl", "nobody"), ("REMOTE", "NOWHERE")))
    assert "storage" in server.reads and server.reads.count("grants") == 1
    return state


def test_one_read_finds_the_users_and_roles_asked_about(live):
    assert (live.storage, live.may_lag, live.sources) == ("local_directory", False, {"REMOTE"})
    assert (set(live.roles), set(live.users)) == ({"analyst"}, {"etl", "analyst"})
    assert live.find("role", "nobody") is None and live.find("user", "stranger") is None


def test_one_read_sorts_the_rows_by_user_and_role(live):
    role = live.find("role", "analyst")
    assert role.holdings.holds(Right("SELECT", Scope("d", "events"), True))
    assert not role.holdings.holds(Right("SELECT", Scope("d", "secrets")))
    user = live.find("user", "etl")
    assert user.holdings.holds(Right("READ", Scope(source="REMOTE")))
    assert user.memberships == (
        Membership("analyst", default=True, admin_option=True),
        Membership("loader", default=False),
    )
    assert (user.default_all, live.find("user", "analyst").default_except) == (False, ("loader",))


def test_no_source_is_asked_about_when_none_is_declared():
    server = FakeServer()
    assert read_live(server, Wanted(("analyst",))).sources == frozenset()
    assert "accepted_sources" not in server.reads


@pytest.mark.parametrize(
    ("storage", "may_lag"),
    [("local_directory", False), ("replicated", True), ("memory", True), (None, True)],
)
def test_only_a_nodes_own_disk_is_never_behind(storage, may_lag):
    """Keeper, a storage the reader doesn't know, and one it can't read may all be behind."""
    live = read_live(FakeServer(storage=storage), Wanted(()))
    assert (live.storage, live.may_lag) == (storage, may_lag)


def test_a_server_error_while_reading_is_an_access_error():
    """Such as a login that can't read `system.grants`: the server's own first line."""
    with pytest.raises(AccessError, match="Not enough privileges"):
        read_live(_Denied(), Wanted(("analyst",)))


class _Denied(FakeServer):
    def _rows(self, name, parameters):
        from clickhouse_connect.driver.exceptions import DatabaseError

        if name == "grants":
            raise DatabaseError(
                "Code: 497. DB::Exception: admin: Not enough privileges. To execute this query, "
                "it's necessary to have the grant SELECT ON system.grants. (ACCESS_DENIED) "
                "(version 26.3.62.3 (official build))"
            )
        return super()._rows(name, parameters)


class _Reads:
    """A reader that is unsettled for its first `empty` reads; `waits` records each sleep.

    Its clock, `now`, moves on with each sleep, and by `read_seconds` with each read.
    """

    def __init__(self, empty, storage="replicated", read_seconds=0.0):
        self.empty, self.storage, self.count, self.waits, self.notices = empty, storage, 0, [], []
        self.now, self.read_seconds = 0.0, read_seconds

    def read(self):
        self.count += 1
        self.now += self.read_seconds
        return LiveState(TREE, self.storage)

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def unsettled(self, state):
        return self.count <= self.empty

    def patience(self, seconds=30.0):
        return Patience(
            seconds=seconds, sleep=self.sleep, clock=lambda: self.now, on_wait=self.notices.append
        )


def test_a_settled_read_is_not_repeated():
    reads = _Reads(empty=0)
    read_settled(reads.read, reads.unsettled, reads.patience())
    assert (reads.count, reads.waits, reads.notices) == (1, [], [])


def test_an_unsettled_read_is_repeated_with_waits_that_double():
    reads = _Reads(empty=8)
    read_settled(reads.read, reads.unsettled, reads.patience())
    assert reads.count == 9
    assert reads.waits == [0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0]
    assert reads.notices == [30.0], "the wait is announced once"


def test_reading_stops_at_the_time_limit():
    reads = _Reads(empty=1000)
    read_settled(reads.read, reads.unsettled, reads.patience(seconds=1.0))
    assert reads.waits == [0.05, 0.1, 0.2, 0.4, pytest.approx(0.25)]
    assert sum(reads.waits) == pytest.approx(1.0) and reads.count == 6


def test_the_time_limit_counts_the_time_reads_take():
    """A 1 s limit with 5 s reads took 31 s when only the waits counted. Now it takes 5 s."""
    reads = _Reads(empty=1000, read_seconds=5.0)
    read_settled(reads.read, reads.unsettled, reads.patience(seconds=1.0))
    assert (reads.count, reads.waits, reads.notices, reads.now) == (1, [], [], 5.0)


def test_slow_reads_stop_once_the_deadline_has_passed():
    """With 0.3 s reads and a 1 s limit, the third read starts at 0.75 s and is the last."""
    reads = _Reads(empty=1000, read_seconds=0.3)
    read_settled(reads.read, reads.unsettled, reads.patience(seconds=1.0))
    assert (reads.count, reads.waits) == (3, [0.05, 0.1])
    assert reads.now == pytest.approx(1.05)


def test_the_time_limit_is_measured_on_the_monotonic_clock():
    """Not wall time, which can jump."""
    assert Patience().clock is time.monotonic


@pytest.mark.parametrize(("seconds", "storage"), [(0, "replicated"), (30, "local_directory")])
def test_no_time_limit_or_a_nodes_own_disk_reads_once(seconds, storage):
    reads = _Reads(empty=1000, storage=storage)
    read_settled(reads.read, reads.unsettled, reads.patience(seconds=seconds))
    assert (reads.count, reads.waits) == (1, [])


@pytest.mark.parametrize("version", ["25.8.1.1", "25.8.33.6", "25.10.2.3", "26.3.62.3", "100.1.1"])
def test_25_8_and_later_are_supported(version):
    require_supported_version(version)


@pytest.mark.parametrize("version", ["24.8.14.39", "25.7.9.9", "25.3.1", "9.10.1.1"])
def test_an_older_server_is_refused_by_its_version(version):
    """Compared as numbers: 25.10 is later than 25.8, and 9.10 is earlier."""
    with pytest.raises(AccessError) as refused:
        require_supported_version(version)
    assert str(refused.value) == (
        f"The server runs ClickHouse {version}. "
        "`bootstrap` and the `access` commands that connect need 25.8 or later."
    )


def test_a_version_that_cannot_be_read_is_refused():
    with pytest.raises(AccessError, match="Can't read the server's version from 'unknown'"):
        require_supported_version("unknown")


def settings(**server):
    return ProjectSettings(
        environment="dev",
        database="shop_dev",
        project="shop",
        migration_user="shop_migrator",
        admin_user="shop_admin",
        server=ServerSettings(**server),
    )


def test_connecting_resolves_only_the_login_secret_and_checks_the_version(monkeypatch):
    import clickhouse_connect

    calls = []

    def get_client(**kwargs):
        calls.append(kwargs)
        return FakeServer()

    monkeypatch.setattr(clickhouse_connect, "get_client", get_client)
    monkeypatch.setenv("CH_DEV_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.delenv("CH_DEV_MIGRATION_PASSWORD", raising=False)
    connect_admin(settings(host="db.example", secure=False))
    connect(settings(host="db.example", port=9440), Login("shop_migrator", "admin_password"))
    assert [(call["host"], call["port"], call["username"], call["secure"]) for call in calls] == [
        ("db.example", 8123, "shop_admin", False),
        ("db.example", 9440, "shop_migrator", True),
    ]
    assert {call["password"] for call in calls} == {"admin-secret"}

    monkeypatch.setattr(FakeServer, "server_version", "24.8.14.39")
    with pytest.raises(AccessError, match="The server runs ClickHouse 24.8.14.39"):
        connect_admin(settings(host="db.example"))


def test_connecting_needs_a_host_and_the_secret(monkeypatch):
    monkeypatch.delenv("CH_DEV_ADMIN_PASSWORD", raising=False)
    with pytest.raises(AccessError, match="host is required in config.yaml for environment 'dev'"):
        connect_admin(settings())
    with pytest.raises(AccessError, match="CH_DEV_ADMIN_PASSWORD is required"):
        connect_admin(settings(host="db.example"))


def write_config(tmp_path, **environment):
    config = {
        "project": {"name": "shop"},
        "defaults": {"secure": False, "aws_region": "eu-west-1"},
        "environments": {"dev": {"database": "shop_dev", "migration_user": "m", **environment}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_settings_read_the_server_keys_with_no_secret(tmp_path):
    path = write_config(tmp_path, host="db.example", port=8124, cluster="main")
    assert load_settings("dev", path).server == ServerSettings(
        host="db.example",
        port=8124,
        secure=False,
        cluster="main",
        aws_region="eu-west-1",
        read_timeout=30.0,
    )
    assert load_settings("dev", write_config(tmp_path)).server.host is None
    timed = load_settings("dev", write_config(tmp_path, access_read_timeout=1.5))
    assert timed.server.read_timeout == 1.5


@pytest.mark.parametrize(
    "value",
    [
        "soon",
        -1,
        True,
        None,
        float("inf"),
        float("nan"),
        pytest.param(int("9" * 400), id="400-digits"),
    ],
)
def test_the_read_timeout_must_be_seconds(tmp_path, value):
    """`.inf` and `.nan` in YAML are floats, but no number of seconds. Neither is an integer
    too large for a float, which would raise OverflowError where seconds are counted."""
    with pytest.raises(SettingsError) as error:
        load_settings("dev", write_config(tmp_path, access_read_timeout=value))
    assert str(error.value) == (
        "access_read_timeout in config.yaml must be a number of seconds, 0 or more"
    )


@pytest.mark.parametrize("value", [None, "main", "it's a cluster"])
def test_the_cluster_is_kept_as_written(tmp_path, value):
    """Its characters aren't checked, because the SQL that names it quotes it."""
    assert load_settings("dev", write_config(tmp_path, cluster=value)).server.cluster == value


@pytest.mark.parametrize("value", [8123, True, False, 0, [], ["main"], {"name": "main"}])
def test_the_cluster_must_be_a_string(tmp_path, value):
    with pytest.raises(SettingsError) as error:
        load_settings("dev", write_config(tmp_path, cluster=value))
    assert str(error.value) == "cluster in config.yaml must be a string"
