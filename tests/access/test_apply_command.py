"""`access apply` without a server: what it refuses, what it sends, and in what order.

The admin's connection is a `FakeServer` that records each write. Exit 2 means that nothing
was written, so every exit-2 test also checks that the server saw no write. Statements
apply sends are checked by text here; `tests/integration/test_access_apply.py` runs them.
"""

import pytest
from click.testing import CliRunner
from fake_access_server import FakeServer, grant_rows

from ch_migrate.access.loader import validate_access
from ch_migrate.access.model import Grant, Target
from ch_migrate.access.settings import load_settings
from ch_migrate.access.templates import statement
from ch_migrate.cli import main

ACCESS_YAML = """\
roles:
  analyst: {grants: ['SELECT, SHOW TABLES ON {db}.*']}
users:
  dashboards: {password: {secret: dashboards_password}, roles: [analyst]}
"""
PASSWORDS = {"DASHBOARDS": "Dash!" + "1a" * 12, "MIGRATION": "Migr!" + "2b" * 12}
IN_SYNC = "✓ In sync for dev (access.yaml): {} applied.\n"


def run(*args):
    result = CliRunner().invoke(main, ["access", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result


@pytest.fixture
def server(project, monkeypatch):
    """A server holding nothing, on each node's own disk; secrets set."""
    (project / "access.yaml").write_text(ACCESS_YAML)
    fake = FakeServer(storage="local_directory")
    monkeypatch.setattr("ch_migrate.access.cli.connect_admin", lambda settings: fake)
    for name, value in PASSWORDS.items():
        monkeypatch.setenv(f"CH_DEV_{name}_PASSWORD", value)
    return fake


@pytest.fixture
def spec(project, server):
    """What ACCESS_YAML declares, as `FakeServer.hold` takes it."""
    return declaration(project)


def declaration(project):
    return validate_access(load_settings("dev", project / "config.yaml"), project).spec


def set_config(project, line):
    """Add one line under the `dev` environment in config.yaml."""
    path = project / "config.yaml"
    path.write_text(
        path.read_text().replace("    host: localhost\n", f"    host: localhost\n{line}\n", 1)
    )


def sent(server):
    return [write.sql for write in server.writes]


def holds_instead(server, grantee, role):
    """The server holds `role`, and none other, where `grantee` holds a role."""
    server.tables["roles"].append({"name": role})
    for row in server.tables["role_grants"]:
        if grantee in (row["user_name"], row["role_name"]):
            row["granted_role_name"] = role


def drop_grants(server, role):
    """The server holds no grant for `role`."""
    server.tables["grants"] = [row for row in server.tables["grants"] if row["role_name"] != role]


def shows_first(server, tables):
    """After `apply`'s writes, its first read shows `tables`, as a replica that is behind
    would, and the reads after it show what the server holds."""
    query, reads = server.query, []

    def lagging(sql, parameters=None, settings=None):
        if server.writes and sql == statement("live", "privileges"):
            reads.append(sql)  # each read starts with the privilege tree
        if len(reads) != 1:
            return query(sql, parameters, settings)
        held, server.tables = server.tables, tables
        try:
            return query(sql, parameters, settings)
        finally:
            server.tables = held

    server.query = lagging


def holds_less_system_users(server, role):
    """`role` holds `SELECT ON {db}.*` instead of its declared table, so narrowing it is a
    replace, and its best-effort `SELECT ON system.*` less `system.users`, which a replace
    can't keep."""
    for row in server.tables["grants"]:
        if row["role_name"] == role and row["database"] == "shop_dev":
            row["table"] = None
    revoked = grant_rows(Grant(("SELECT",), Target("system", "users")), is_partial_revoke=1)
    server.tables["grants"] += [{"user_name": None, "role_name": role, **row} for row in revoked]


def test_in_sync_sends_nothing_and_reports_0_changes(server, spec):
    server.hold(spec)
    result = run("apply", "dev")
    assert (result.exit_code, result.stdout, result.stderr) == (0, IN_SYNC.format("0 changes"), "")
    assert server.writes == []


def test_creates_come_first_and_passwords_are_bound_never_written(server, spec):
    server.converges_to = spec
    result = run("apply", "dev")
    assert result.exit_code == 0, result.output
    assert sent(server)[:5] == [
        "CREATE ROLE IF NOT EXISTS `shop_migration_role`",
        "CREATE ROLE IF NOT EXISTS `analyst`",
        "CREATE USER IF NOT EXISTS `shop_migrator` IDENTIFIED BY {password:String}",
        "CREATE USER IF NOT EXISTS `dashboards` IDENTIFIED BY {password:String}",
        "GRANT `shop_migration_role` TO `shop_migrator`",
    ]
    assert [write.parameters for write in server.writes[2:4]] == [
        {"password": PASSWORDS["MIGRATION"]},
        {"password": PASSWORDS["DASHBOARDS"]},
    ]
    # Only the password statements go by raw_query, form-encoded.
    assert server.raw == sent(server)[2:4]
    assert "GRANT CURRENT GRANTS(CLUSTER ON *.*) TO `shop_migration_role`" in sent(server)
    assert not any(password in result.output for password in PASSWORDS.values())
    assert result.stdout.splitlines()[-1] == IN_SYNC.format("15 changes").strip()


def test_cluster_granted_to_every_login_is_never_tried(server, spec):
    """Where the server grants CLUSTER to every login, as ClickHouse Cloud does, the
    migration role already holds it: `apply` sends no `CURRENT GRANTS(CLUSTER ...)` and prints
    no best-effort line for it, on any run."""
    server.own_grants = []
    server.converges_to = spec
    result = run("apply", "dev")
    assert result.exit_code == 0, result.output
    assert not any("CLUSTER" in sql for sql in sent(server))
    assert "CLUSTER" not in result.output


def test_an_unset_secret_exits_2_before_any_write(server, monkeypatch):
    monkeypatch.delenv("CH_DEV_DASHBOARDS_PASSWORD")
    result = run("apply", "dev")
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr.startswith("✗ CH_DEV_DASHBOARDS_PASSWORD is required.")
    assert server.writes == []


def test_the_login_apply_runs_as_is_never_declared(server):
    server.login = "dashboards"
    result = run("apply", "dev")
    assert (result.exit_code, result.stdout, server.writes) == (2, "", []), result.output
    assert result.stderr == (
        "✗ `apply` is logged in as dashboards, which access.yaml declares. "
        "ch-migrate never changes the login it runs as.\n"
    )


def test_apply_never_takes_access_from_a_role_the_login_holds(server, spec, project):
    """A revoke from a role the admin holds could cut a right a later
    statement needs. Holding a declared role is fine: granting it needs ADMIN OPTION."""
    server.hold(spec)
    server.login_roles = ["analyst"]
    assert run("apply", "dev").exit_code == 0 and server.writes == []
    narrower = ACCESS_YAML.replace("SELECT, SHOW TABLES ON {db}.*", "SELECT ON {db}.*")
    (project / "access.yaml").write_text(narrower)  # revokes SHOW TABLES from analyst
    result = run("apply", "dev")
    assert (result.exit_code, server.writes) == (2, []), result.output
    assert (
        "✗ access.yaml would revoke from, replace or drop role analyst, which the login "
        "`apply` runs as holds."
    ) in result.stderr


def test_a_migration_user_that_is_the_admin_exits_2(server, project):
    path = project / "config.yaml"
    path.write_text(path.read_text().replace("admin_user: shop_admin", "admin_user: shop_migrator"))
    result = run("apply", "dev")
    assert (result.exit_code, result.stdout, server.writes) == (2, "", []), result.output
    assert result.stderr.startswith(
        "✗ The migration user shop_migrator is config.yaml's admin_user"
    )


def test_a_plan_error_exits_2_before_any_write(server, project):
    (project / "access.yaml").write_text("users: {nopass: {}}\n")
    result = run("apply", "dev")
    assert (result.exit_code, result.stdout, server.writes) == (2, "", []), result.output
    assert "user nopass isn't on the server and has no password" in result.stderr


def test_a_failed_membership_keeps_the_role_it_replaces(server, spec):
    """2.5: a user moving from role `old` to `analyst` keeps `old` if `analyst` fails."""
    server.hold(spec)
    server.tables["roles"].append({"name": "old"})
    for row in server.tables["role_grants"]:
        if row["user_name"] == "dashboards":
            row["granted_role_name"] = "old"
    server.failing = {
        "GRANT `analyst` TO `dashboards`": "Code: 497. DB::Exception: no. (ACCESS_DENIED)"
    }
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert sent(server) == ["GRANT `analyst` TO `dashboards`"]
    assert result.stderr.splitlines()[:2] == [
        "✗ grant role analyst to user dashboards: no. (ACCESS_DENIED)",
        "! skipped: revoke role old from user dashboards, because a change to user dashboards "
        "failed, so it keeps the access it has",
    ]
    assert "- revoke role old from user dashboards" in result.stdout.splitlines()


@pytest.mark.parametrize("failing", ["analyst", "base"])
def test_a_failed_role_keeps_its_holders_old_access(server, project, failing):
    """2.5: a failed change to a role counts for every declared holder, directly or through
    declared roles. The user moving from `old` to `analyst`, which holds `base`, keeps `old`."""
    (project / "access.yaml").write_text(
        "roles:\n"
        "  base: {grants: ['SELECT ON {db}.events']}\n"
        "  analyst: {grants: ['SELECT ON {db}.regions'], roles: [base]}\n"
        "users:\n"
        "  dashboards: {password: {secret: dashboards_password}, roles: [analyst]}\n"
    )
    server.hold(declaration(project))
    holds_instead(server, "dashboards", "old")
    drop_grants(server, failing)
    server.failing = {f"TO `{failing}`": "Code: 497. DB::Exception: no. (ACCESS_DENIED)"}
    table = {"analyst": "regions", "base": "events"}[failing]
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert sent(server)[0] == "GRANT `analyst` TO `dashboards`"
    assert result.stderr.splitlines()[:2] == [
        f"✗ grant SELECT ON shop_dev.{table} to role {failing}: no. (ACCESS_DENIED)",
        "! skipped: revoke role old from user dashboards, because a change to role "
        f"{failing}, which it holds, failed, so it keeps the access it has",
    ]
    assert not any(sql.startswith("REVOKE") for sql in sent(server))


def test_a_failed_sibling_role_holds_back_the_revoke(server, project):
    """`dashboards` holds `old` and `new`, and access.yaml moves its
    read on events from `old` to `new`. Granting it to `new` fails, so `old` keeps it."""
    holding = "users:\n  dashboards: {password: {secret: dashboards_password}, roles: [old, new]}\n"
    (project / "access.yaml").write_text(
        "roles:\n  old: {grants: ['SELECT ON {db}.events']}\n  new: {}\n" + holding
    )
    server.hold(declaration(project))
    (project / "access.yaml").write_text(
        "roles:\n  old: {}\n  new: {grants: ['SELECT ON {db}.*']}\n" + holding
    )
    server.failing = {"TO `new`": "Code: 497. DB::Exception: no. (ACCESS_DENIED)"}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert not any(sql.startswith("REVOKE") for sql in sent(server)), sent(server)
    assert (
        "! skipped: revoke SELECT ON shop_dev.events from role old, because an earlier change "
        "to role new failed, so it keeps the access it has"
    ) in result.stderr.splitlines()


@pytest.mark.parametrize(
    ("failing", "reason"),
    [
        ("GRANT `base` TO `reader`", "a change to role reader failed"),
        (" TO `base`", "a change to role base, which it holds, failed"),
    ],
)
def test_a_replace_waits_for_its_own_changes_and_its_roles(server, project, failing, reason):
    """2.5: a replace is gated like a revoke: an earlier failure for the role, or for a role it
    holds, skips it, so the role keeps what it holds."""
    (project / "access.yaml").write_text(
        "roles:\n"
        "  base: {grants: ['SELECT ON {db}.regions']}\n"
        "  reader: {grants: ['SELECT ON {db}.events'], roles: [base]}\n"
    )
    server.hold(declaration(project))
    for row in server.tables["grants"]:
        if row["role_name"] == "reader":
            row["table"] = None  # it holds SELECT ON {db}.*: narrowing is a replace
    if failing.startswith("GRANT `base`"):
        server.tables["role_grants"] = [
            row for row in server.tables["role_grants"] if row["role_name"] != "reader"
        ]
    else:
        drop_grants(server, "base")
    server.failing = {failing: "Code: 497. DB::Exception: no. (ACCESS_DENIED)"}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert not any("REPLACE OPTION" in sql for sql in sent(server))
    assert (
        "! skipped: replace the grants of role reader with its declared set in one statement, "
        "instead of: revoke SELECT ON shop_dev.* from role reader, because "
        f"{reason}, so it keeps the access it has"
    ) in result.stderr.splitlines()


def test_a_role_is_replaced_before_its_holders(server, project):
    """`reader`, declared first, holds `base`, and both narrow a scope.
    Base's replace runs first and fails, so reader's is skipped and named."""
    (project / "access.yaml").write_text(
        "roles:\n"
        "  reader: {grants: ['SELECT ON {db}.events'], roles: [base]}\n"
        "  base: {grants: ['SELECT ON {db}.regions']}\n"
    )
    server.hold(declaration(project))
    for row in server.tables["grants"]:
        if row["role_name"] in ("reader", "base"):
            row["table"] = None  # each holds SELECT ON {db}.*: narrowing is a replace
    denied = "Code: 497. DB::Exception: no. (ACCESS_DENIED)"
    server.failing = {"TO `base` WITH REPLACE OPTION": denied}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert [sql for sql in sent(server) if "REPLACE OPTION" in sql] == [
        "GRANT SELECT ON `shop_dev`.`regions` TO `base` WITH REPLACE OPTION"
    ]
    assert (
        "! skipped: replace the grants of role reader with its declared set in one statement, "
        "instead of: revoke SELECT ON shop_dev.* from role reader, because a change to role "
        "base, which it holds, failed, so it keeps the access it has"
    ) in result.stderr.splitlines()


def test_a_replace_that_cannot_keep_a_revoke_is_skipped_and_holds_back_its_holders(server, project):
    """`reader` holds best-effort `SELECT ON system.*` less `system.users`,
    and narrows a scope. Its replace isn't sent, and the user moving to it keeps `old`."""
    (project / "access.yaml").write_text(
        "roles:\n"
        "  reader:\n"
        "    grants: ['SELECT ON {db}.events', {grant: SELECT ON system.*, best_effort: true}]\n"
        "users:\n"
        "  dashboards: {password: {secret: dashboards_password}, roles: [reader]}\n"
    )
    server.hold(declaration(project))
    holds_instead(server, "dashboards", "old")
    for row in server.tables["grants"]:
        if row["role_name"] == "reader" and row["database"] == "shop_dev":
            row["table"] = None  # it holds SELECT ON {db}.*: narrowing is a replace
    revoked = grant_rows(Grant(("SELECT",), Target("system", "users")), is_partial_revoke=1)
    server.tables["grants"] += [
        {"user_name": None, "role_name": "reader", **row} for row in revoked
    ]
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert not any("REPLACE OPTION" in sql or sql.startswith("REVOKE") for sql in sent(server))
    assert result.stderr.splitlines()[:2] == [
        "! skipped: replace the grants of role reader with its declared set in one statement, "
        "instead of: revoke SELECT ON shop_dev.* from role reader, because what it holds of the "
        "best-effort grant SELECT ON system.* has SELECT ON system.users revoked inside it, "
        "which a replace can't keep, so it keeps the access it has",
        "! skipped: revoke role old from user dashboards, because a change to role reader, "
        "which it holds, couldn't be sent, so it keeps the access it has",
    ]


def test_a_replace_that_cannot_be_sent_holds_back_the_drops(server, project):
    """The user moves from `old`, declared `absent: true`, to `reader`,
    whose replace can't keep a revoke inside its best-effort grant. Nothing failed, but the
    revoke of `old` and its drop are both skipped and named, so the user keeps `old`."""
    declared = (
        "roles:\n"
        "  reader:\n"
        "    grants: ['SELECT ON {db}.events', {grant: SELECT ON system.*, best_effort: true}]\n"
        "users:\n"
        "  dashboards: {password: {secret: dashboards_password}, roles: [reader]}\n"
    )
    (project / "access.yaml").write_text(declared)
    server.hold(declaration(project))
    holds_instead(server, "dashboards", "old")
    holds_less_system_users(server, "reader")
    absent = declared.replace("users:\n", "  old: {absent: true}\nusers:\n")
    (project / "access.yaml").write_text(absent)
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert "GRANT `reader` TO `dashboards`" in sent(server)
    assert not any(sql.startswith(("DROP", "REVOKE")) for sql in sent(server)), sent(server)
    skipped = [line for line in result.stderr.splitlines() if line.startswith("! skipped: ")]
    assert skipped[0].startswith("! skipped: replace the grants of role reader "), skipped
    assert skipped[1:] == [
        "! skipped: revoke role old from user dashboards, because a change to role reader, "
        "which it holds, couldn't be sent, so it keeps the access it has",
        "! skipped: drop role old, because the replace of role reader couldn't be sent",
    ]


@pytest.mark.parametrize(
    ("failing", "reason"),
    [
        ("GRANT `analyst` TO `dashboards`", "a change to user dashboards failed"),
        (" TO `analyst`", "a change to role analyst, which it holds, failed"),
    ],
)
def test_default_roles_wait_like_revokes(server, project, failing, reason):
    """A user moving from `old` to `analyst`, its new default role, keeps
    `old` active at login if granting `analyst`, or analyst's own grant, fails."""
    (project / "access.yaml").write_text(
        ACCESS_YAML.replace("roles: [analyst]}", "roles: [analyst], default_roles: [analyst]}")
    )
    server.hold(declaration(project))
    holds_instead(server, "dashboards", "old")
    for row in server.tables["users"]:
        if row["name"] == "dashboards":
            row["default_roles_all"] = 0  # only `old`, which role_grants marks as default
    drop_grants(server, "analyst")
    server.failing = {failing: "Code: 497. DB::Exception: no. (ACCESS_DENIED)"}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert not any("DEFAULT ROLE" in sql for sql in sent(server))
    assert (
        "! skipped: set the default roles of user dashboards to analyst, because "
        f"{reason}, so it keeps the access it has"
    ) in result.stderr.splitlines()


def test_drops_wait_until_nothing_has_failed(server, spec, project):
    server.hold(spec)
    server.tables["users"].append(
        {"name": "gone", "default_roles_all": 1, "default_roles_except": []}
    )
    (project / "access.yaml").write_text(
        ACCESS_YAML + "  gone: {absent: true}\n  x: {password: {secret: dashboards_password}}\n"
    )
    server.failing = {
        "CREATE USER IF NOT EXISTS `x`": "Code: 497. DB::Exception: no. (ACCESS_DENIED)"
    }
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert not any(sql.startswith("DROP") for sql in sent(server))
    assert (
        "! skipped: drop user gone, because an earlier change failed" in result.stderr.splitlines()
    )


def test_on_cluster_where_users_are_on_each_node(server, project):
    set_config(project, "    cluster: it_cluster")
    assert run("apply", "dev").exit_code == 1  # the fake never converges
    assert server.writes and all(" ON CLUSTER `it_cluster`" in sql for sql in sent(server))
    assert all(write.settings["wait_end_of_query"] == 1 for write in server.writes)


def test_on_a_cluster_each_best_effort_privilege_is_its_own_grant(server, spec, project):
    """Decision 12: never CURRENT GRANTS ON CLUSTER. A refused privilege is a warning, and the
    privileges after it are still sent."""
    set_config(project, "    cluster: it_cluster")
    server.converges_to = spec
    refusal = "Code: 497. DB::Exception: shop_admin: Not enough privileges. (ACCESS_DENIED)"
    server.failing = {" CLUSTER ON *.* TO ": refusal}
    result = run("apply", "dev")
    assert result.exit_code == 0, result.output
    assert not any("CURRENT GRANTS" in sql for sql in sent(server))
    best_effort = [
        "SELECT ON `system`.`parts`",
        "SELECT ON `system`.`replicas`",
        "SYSTEM SYNC DATABASE REPLICA ON `shop_dev`.*",
        "SYSTEM SYNC REPLICA ON `shop_dev`.*",
        "CLUSTER ON *.*",
        "READ ON REMOTE",
        "CREATE TEMPORARY TABLE ON *.*",
    ]
    expected = [
        f"GRANT ON CLUSTER `it_cluster` {grant} TO `shop_migration_role`" for grant in best_effort
    ]
    assert [sql for sql in sent(server) if sql in expected] == expected
    assert result.stderr == (
        "! grant role shop_migration_role as much of CLUSTER ON *.* as the admin holds "
        "(best-effort): shop_admin: Not enough privileges. (ACCESS_DENIED)\n"
    )


def test_no_on_cluster_where_users_are_in_keeper(server, project):
    set_config(project, "    cluster: it_cluster")
    set_config(project, "    access_read_timeout: 0")  # nothing here would ever show up
    server.storage = "replicated"
    run("apply", "dev")
    assert server.writes and not any("ON CLUSTER" in sql for sql in sent(server))
    assert all(write.settings == {} for write in server.writes)


def test_a_cluster_with_unknown_storage_exits_2(server, project):
    set_config(project, "    cluster: it_cluster")
    set_config(project, "    access_read_timeout: 0")  # unknown storage may lag: don't wait
    server.storage = None
    result = run("apply", "dev")
    assert (result.exit_code, server.writes) == (2, []), result.output
    assert "Grant the admin SELECT ON system.user_directories." in result.stderr


def test_reads_again_while_a_changed_role_still_differs(server, project):
    """A replica shows only the first statement of a mixed-option
    replace, then all of it. What is pending for the role then is a grant option, not the
    replace `apply` made, and `apply` still reads again, and ends in sync.

    No cluster holds the node, so the node is read on its own, and the reason is said once,
    though `apply` reads before and after its writes."""
    (project / "access.yaml").write_text(
        "roles:\n"
        "  mixed: {grants: ['SELECT ON {db}.events', 'INSERT ON {db}.events WITH GRANT OPTION']}\n"
    )
    spec = declaration(project)
    server.storage, server.clusters = "replicated", []
    server.hold(spec)
    for row in server.tables["grants"]:
        if row["role_name"] == "mixed":
            row.update(table=None, grant_option=1)  # SELECT, INSERT ON {db}.*: a replace
    first = FakeServer()
    first.hold(spec)
    for row in first.tables["grants"]:
        if row["role_name"] == "mixed":
            row["grant_option"] = 0  # the replace's first statement: the set, no option
    server.converges_to = spec
    shows_first(server, first.tables)
    result = run("apply", "dev")
    assert result.exit_code == 0, result.output
    assert sent(server) == [
        "GRANT SELECT ON `shop_dev`.`events`, INSERT ON `shop_dev`.`events` TO `mixed` "
        "WITH REPLACE OPTION",
        "GRANT INSERT ON `shop_dev`.`events` TO `mixed` WITH GRANT OPTION",
    ]
    assert "Reading again for up to" in result.stderr
    assert result.stderr.count("Can't read every replica at once") == 1
    assert result.stdout.splitlines()[-1] == IN_SYNC.format("1 change").strip()


def test_a_change_that_failed_is_not_read_for_again(server, project):
    """A change `apply` failed at stays pending however long it reads, so it isn't read for
    again, although another change to the same role landed."""
    set_config(project, "    access_read_timeout: 1")
    server.storage, server.clusters = "replicated", []
    grants = ["SELECT ON {db}.events", "INSERT ON {db}.regions", "INSERT ON {db}.events"]
    server.hold(declared_role(project, grants))
    server.converges_to = declared_role(project, grants[:2])  # once INSERT ON events is revoked
    declared_role(project, grants[:1])
    revoking = "REVOKE INSERT ON `shop_dev`.`regions`"
    server.failing = {revoking: "Code: 497. DB::Exception: no. (ACCESS_DENIED)"}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert sent(server) == [
        "REVOKE INSERT ON `shop_dev`.`events` FROM `reader`",
        f"{revoking} FROM `reader`",
    ]
    assert "Reading again" not in result.stderr
    assert "✗ revoke INSERT ON shop_dev.regions from role reader: no. (ACCESS_DENIED)" in (
        result.stderr.splitlines()
    )


def test_ctrl_c_after_a_write_names_the_stop_and_exits_130(server):
    def interrupt(sql, parameters=None, settings=None):
        answer = FakeServer.command(server, sql, parameters, settings)
        if server.writes:  # a CHECK GRANT while reading isn't a write
            raise KeyboardInterrupt
        return answer

    server.command = interrupt
    result = run("apply", "dev")
    assert result.exit_code == 130, result.output
    assert "✗ `apply` stopped part-way: interrupted." in result.stderr
    assert "ch-migrate access plan shows what still differs." in result.stderr


def declared_role(project, grants):
    """Declare only role `reader`, with these grants, and return the declaration."""
    (project / "access.yaml").write_text(f"roles:\n  reader: {{grants: {grants}}}\n")
    return declaration(project)


def test_once_a_write_has_run_an_error_exits_1_not_2(server, monkeypatch):
    def broken_read(reader, wanted):
        raise RuntimeError("connection reset")

    def write_then_break(sql, parameters=None, settings=None):
        answer = FakeServer.command(server, sql, parameters, settings)
        if server.writes:
            monkeypatch.setattr("ch_migrate.access.live.Reader.read", broken_read)
        return answer

    server.command = write_then_break
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert "✗ `apply` stopped part-way: RuntimeError: connection reset" in result.stderr


def test_reset_passwords_only_when_asked(server, spec):
    server.hold(spec)
    assert run("apply", "dev").exit_code == 0 and server.writes == []
    result = run("apply", "dev", "--reset-passwords")
    assert result.exit_code == 0, result.output
    assert [(w.sql, w.parameters) for w in server.writes] == [
        (
            "ALTER USER `dashboards` IDENTIFIED BY {password:String}",
            {"password": PASSWORDS["DASHBOARDS"]},
        ),
    ]
    assert result.stdout == "+ reset the password of user dashboards\n" + IN_SYNC.format("1 change")


def test_reset_passwords_also_resets_a_user_this_run_creates(server, spec):
    """Someone else could make the user between apply's read and its
    `CREATE USER IF NOT EXISTS`, which then keeps their password; the reset replaces it."""
    server.hold(spec)
    server.tables["users"] = [row for row in server.tables["users"] if row["name"] != "dashboards"]
    server.tables["role_grants"] = [
        row for row in server.tables["role_grants"] if row["user_name"] != "dashboards"
    ]
    server.converges_to = spec
    result = run("apply", "dev", "--reset-passwords")
    assert result.exit_code == 0, result.output
    assert sent(server)[:3] == [
        "CREATE USER IF NOT EXISTS `dashboards` IDENTIFIED BY {password:String}",
        "ALTER USER `dashboards` IDENTIFIED BY {password:String}",
        "GRANT `analyst` TO `dashboards`",
    ]
    assert server.writes[1].parameters == {"password": PASSWORDS["DASHBOARDS"]}


def test_a_reset_is_skipped_when_its_create_failed(server, spec):
    """The reset would fail too, as a second error for one problem."""
    server.hold(spec)
    server.tables["users"] = [row for row in server.tables["users"] if row["name"] != "dashboards"]
    server.failing = {"CREATE USER IF NOT EXISTS `dashboards`": "Code: 497. no. (ACCESS_DENIED)"}
    result = run("apply", "dev", "--reset-passwords")
    assert result.exit_code == 1, result.output
    assert not any(sql.startswith("ALTER USER `dashboards`") for sql in sent(server))
    assert (
        "! skipped: reset the password of user dashboards, because its create failed, "
        "so this run gives it nothing more" in result.stderr.splitlines()
    )


def test_a_failed_create_holds_back_every_change_to_its_user(server, spec):
    """An admin with ALTER USER and GRANT but not CREATE USER
    fails the create even when someone else has made the user in the window. Their user keeps
    their password, so it must not get the grants."""
    server.hold(spec)
    server.tables["users"] = [row for row in server.tables["users"] if row["name"] != "dashboards"]
    server.tables["role_grants"] = [
        row for row in server.tables["role_grants"] if row["user_name"] != "dashboards"
    ]
    server.failing = {"CREATE USER IF NOT EXISTS `dashboards`": "Code: 497. no. (ACCESS_DENIED)"}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert not any(sql.startswith("GRANT `analyst` TO `dashboards`") for sql in sent(server))
    assert (
        "! skipped: grant role analyst to user dashboards, because its create failed, "
        "so this run gives it nothing more" in result.stderr.splitlines()
    )


def test_a_failed_reset_holds_back_every_change_to_its_user(server, spec):
    """An admin without ALTER USER, or a user someone else made between the
    read and the create, keeps a password this run didn't set: it gets no new access, and no
    revoke runs anywhere."""
    server.hold(spec)
    holds_instead(server, "dashboards", "old")
    server.failing = {"ALTER USER `dashboards`": "Code: 497. no. (ACCESS_DENIED)"}
    result = run("apply", "dev", "--reset-passwords")
    assert result.exit_code == 1, result.output
    assert sent(server) == ["ALTER USER `dashboards` IDENTIFIED BY {password:String}"]
    skipped = [line for line in result.stderr.splitlines() if line.startswith("! skipped: ")]
    assert skipped == [
        "! skipped: grant role analyst to user dashboards, because its password reset failed, "
        "so this run gives it nothing more",
        "! skipped: revoke role old from user dashboards, because its password reset failed, "
        "so this run gives it nothing more",
    ]


def test_a_user_made_in_the_window_gets_no_grant_when_its_reset_fails(server, spec):
    """The create keeps someone else's password, and the reset
    that would replace it fails, so the user is granted nothing."""
    server.hold(spec)
    server.tables["users"] = [row for row in server.tables["users"] if row["name"] != "dashboards"]
    server.tables["role_grants"] = [
        row for row in server.tables["role_grants"] if row["user_name"] != "dashboards"
    ]
    server.failing = {"ALTER USER `dashboards`": "Code: 497. no. (ACCESS_DENIED)"}
    result = run("apply", "dev", "--reset-passwords")
    assert result.exit_code == 1, result.output
    assert not any(sql.startswith("GRANT `analyst` TO `dashboards`") for sql in sent(server))


def test_a_failure_shows_the_servers_message_with_no_secret(server, spec):
    message = (
        "Received ClickHouse exception, code: 36, server response: Code: 36. DB::Exception: "
        f"bad password {PASSWORDS['DASHBOARDS']}. (BAD_ARGUMENTS) (for url http://localhost:8123)"
    )
    server.failing = {"CREATE USER IF NOT EXISTS `dashboards`": message}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert (
        "✗ create user dashboards: bad password [hidden]. (BAD_ARGUMENTS)"
        in result.stderr.splitlines()
    )


def test_an_empty_secret_hides_nothing(server, monkeypatch):
    """A secret can be empty, as an SSM parameter can. Hiding "" would mark every gap."""
    monkeypatch.setattr("ch_migrate.access.applier.read_secret", lambda settings, name: "")
    message = "Code: 36. DB::Exception: bad password. (BAD_ARGUMENTS)"
    server.failing = {"CREATE USER IF NOT EXISTS `dashboards`": message}
    result = run("apply", "dev")
    assert result.exit_code == 1, result.output
    assert "✗ create user dashboards: bad password. (BAD_ARGUMENTS)" in result.stderr.splitlines()
