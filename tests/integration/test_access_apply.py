"""`access apply` against a real server (design note, 2.5).

Every user and role a test creates starts with the project's `it_<hex>` name, so the access
fixtures' finalizer drops them; the cluster tests drop theirs on every node. What `apply`
sent is read back from `system.query_log`, by the login that sent it, so "no write" and "one
statement" are what the server saw.

`apply` turns an error after its first write into exit 1, and one before it into exit 2.
Neither shows as an exception, so every exit-1 and exit-2 assertion names the lines it
expects, and `apply()` fails the test if `apply` stopped part-way.
"""

import os
import secrets
import threading
import time
from dataclasses import replace

import pytest
from access_fixtures import (
    AccessProject,
    initialize_cluster_project,
    query_log,
    read_access,
    requires_cluster_grant,
)
from clickhouse_connect.driver.exceptions import DatabaseError
from test_access_plan import ACCESS_YAML, access, marked, run_all, show_access, write_access
from test_access_spike import SMALLEST_ADMIN

from ch_migrate.access.live import LiveState
from ch_migrate.access.loader import validate_access
from ch_migrate.access.model import Grant, Target
from ch_migrate.access.planner import Change, Entity
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.settings import load_settings
from ch_migrate.access.writes import Writing, writes_for

pytestmark = pytest.mark.integration

# What the smallest admin (U6) lacks to read access: each ACCESS_DENIED on 26.3 names one.
# Where users and roles are replicated, as on ClickHouse Cloud, READ ON REMOTE reads every
# replica at once, through the cluster system.clusters names (design note, 2.5).
READ_GRANTS = (
    *(
        f"SELECT ON system.{table}"
        for table in ("users", "roles", "grants", "role_grants", "user_directories", "clusters")
    ),
    "READ ON REMOTE",
)
# An admin with some of core's best-effort grants (Decision 12): the smallest admin, the
# grants it reads access with, CLUSTER without grant option, which ON CLUSTER itself needs,
# and two best-effort grants it can pass on. Its CREATE TABLE ON {db}.* WITH GRANT OPTION
# also lets a plain GRANT pass on a third, CREATE TEMPORARY TABLE ON *.*, on 26.3.
PARTIAL_ADMIN = (
    *SMALLEST_ADMIN,
    *READ_GRANTS,
    "CLUSTER ON *.*",
    "SELECT ON system.parts WITH GRANT OPTION",
    "SYSTEM SYNC REPLICA ON {db}.* WITH GRANT OPTION",
)
# The secrets ACCESS_YAML's users read.
SECRETS = ("dashboards_password", "etl_password")
PLAN_IN_SYNC = "✓ In sync for it (access.yaml): nothing to change.\n"


def test_converges_on_an_empty_server_then_plan_is_in_sync(access_project, monkeypatch):
    project, p = access_project, access_project.name
    write_access(project, ACCESS_YAML)
    passwords = set_secrets(project, monkeypatch, *SECRETS)
    result = apply(project)
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    lines = result.stdout.splitlines()
    assert lines[:3] == [
        f"+ create role {p}_migration_role",
        f"+ create role {p}_analyst",
        f"+ create role {p}_loader",
    ]
    assert lines[-1] == "✓ In sync for it (access.yaml): 25 changes applied."
    assert_no_secret(result, passwords)
    assert_plan_in_sync(project)


def test_a_second_apply_reports_0_changes_and_writes_nothing(
    access_project, clickhouse_server, monkeypatch
):
    """Its admin holds every best-effort grant, so no `CURRENT GRANTS` try is left to make."""
    project = access_project
    write_access(project, ACCESS_YAML)
    set_secrets(project, monkeypatch, *SECRETS)
    login = admin_login(project, clickhouse_server, ("CURRENT GRANTS ON *.* WITH GRANT OPTION",))
    use_admin(project, login, monkeypatch)
    assert apply(project).exit_code == 0
    since = server_time(project.client)
    result = apply(project)
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert result.stdout == "✓ In sync for it (access.yaml): 0 changes applied.\n"
    assert written(project.client, login.user, since) == []


def test_drift_in_grants_and_memberships_is_undone(access_project, monkeypatch):
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    set_secrets(project, monkeypatch, *SECRETS)
    assert apply(project).exit_code == 0
    run_all(
        project,
        f"GRANT INSERT ON {db}.* TO {p}_analyst",
        f"REVOKE SELECT ON {db}.regions FROM {p}_etl",
        f"REVOKE {p}_reporting FROM {p}_dashboards",
        f"GRANT {p}_loader TO {p}_dashboards",
    )
    assert access(project, "plan", "it").exit_code == 1
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert marked(result, "+") == [
        f"grant role {p}_reporting to user {p}_dashboards",
        f"grant SELECT ON {db}.regions to user {p}_etl",
    ]
    assert marked(result, "-") == [
        f"revoke INSERT ON {db}.* from role {p}_analyst",
        f"revoke role {p}_loader from user {p}_dashboards",
    ]
    assert_plan_in_sync(project)


def test_absent_drops_the_user_and_role_and_leaves_an_undeclared_user_alone(
    access_project, monkeypatch
):
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    set_secrets(project, monkeypatch, *SECRETS)
    assert apply(project).exit_code == 0
    stranger = f"{p}_stranger"
    run_all(
        project,
        f"CREATE USER {stranger} IDENTIFIED BY '{project.new_password()}'",
        f"GRANT {p}_analyst TO {stranger}",
        f"GRANT SELECT ON {db}.regions TO {stranger}",
    )
    before = read_access(project.client, [stranger], [])
    gone = f"environments: {{it: {{users: {{'{{project}}_etl': {{absent: true}}}}, "
    gone += "roles: {'{project}_loader': {absent: true}}}}\n"
    write_access(project, ACCESS_YAML + gone)
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert marked(result, "-") == [f"drop user {p}_etl", f"drop role {p}_loader"]
    assert existing(project.client, "users", f"{p}_etl") == []
    assert existing(project.client, "roles", f"{p}_loader") == []
    assert read_access(project.client, [stranger], []) == before
    assert_plan_in_sync(project)


def test_adopting_a_role_revokes_its_extra_grants(access_project):
    project, p, db = access_project, access_project.name, access_project.database
    analyst = f"{p}_analyst"
    write_access(
        project, f"roles: {{{analyst}: {{grants: ['SELECT, SHOW TABLES ON {{db}}.*']}}}}\n"
    )
    run_all(
        project,
        f"CREATE ROLE {analyst}",
        f"GRANT SELECT, SHOW TABLES, INSERT ON {db}.* TO {analyst}",
        f"GRANT CREATE ROLE ON *.* TO {analyst}",
    )
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    adopt = f"adopt role {analyst}: it exists on the server, and access.yaml now manages it"
    assert marked(planned, "~") == [adopt]
    extras = [
        f"revoke CREATE ROLE ON *.* from role {analyst}",
        f"revoke INSERT ON {db}.* from role {analyst}",
    ]
    assert marked(planned, "-") == extras
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert marked(result, "~") == [adopt] and marked(result, "-") == extras
    assert_plan_in_sync(project)


def test_narrowing_a_privilege_leaves_the_user_only_what_is_declared(
    access_project, clickhouse_server, monkeypatch
):
    """`ALTER` held, `ALTER UPDATE` declared: the user can still update, and no longer delete."""
    project, p, db = access_project, access_project.name, access_project.database
    writer = f"{p}_writer"
    [password] = set_secrets(project, monkeypatch, "writer_password").values()
    declare = (
        "users: {{'{{project}}_writer': "
        "{{password: {{secret: writer_password}}, grants: ['{} ON {{db}}.*']}}}}\n"
    )
    run_all(
        project,
        f"CREATE DATABASE {db}",
        f"CREATE TABLE {db}.events (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id",
        f"INSERT INTO {db}.events VALUES (1, 0)",
    )
    write_access(project, declare.format("ALTER"))
    assert apply(project).exit_code == 0
    user = replace(clickhouse_server, user=writer, password=password).connect()
    try:
        user.command(f"ALTER TABLE {db}.events DELETE WHERE id = 0")
        write_access(project, declare.format("ALTER UPDATE"))
        result = apply(project)
        assert result.exit_code == 0, result.output
        assert marked(result, "-") and not marked(result, "+"), result.output
        user.command(f"ALTER TABLE {db}.events UPDATE x = 1 WHERE id = 1")
        with pytest.raises(DatabaseError) as denied:
            user.command(f"ALTER TABLE {db}.events DELETE WHERE id = 1")
        assert denied.value.code == 497, denied.value
    finally:
        user.close()
    assert_plan_in_sync(project)


def test_narrowing_a_scope_is_one_replace_that_keeps_memberships(
    access_project, clickhouse_server, monkeypatch
):
    """Decision 3, run as the smallest admin (U6) plus the grants it needs to read access."""
    project, p, db = access_project, access_project.name, access_project.database
    reader = narrowed_reader(project)
    login = admin_login(project, clickhouse_server, SMALLEST_ADMIN + READ_GRANTS)
    use_admin(project, login, monkeypatch)
    since = server_time(project.client)
    result = apply(project)
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert marked(result, "-") == [
        f"replace the grants of role {reader} with its declared set in one statement, "
        f"instead of: revoke SELECT ON {db}.* from role {reader}"
    ]
    assert written(project.client, login.user, since) == [
        f"GRANT SELECT ON `{db}`.`events` TO `{reader}` WITH REPLACE OPTION"
    ]
    assert memberships(project.client, reader) == [f"{p}_base"]
    assert_plan_in_sync(project)


def test_a_replace_the_admin_cannot_make_changes_nothing(
    access_project, clickhouse_server, monkeypatch
):
    """The role holds a right the admin can't revoke: the replace fails as one statement."""
    project, p = access_project, access_project.name
    reader = narrowed_reader(project)
    project.client.command(f"GRANT SELECT ON {p}_other.* TO {reader}")
    before = read_access(project.client, [], [reader])
    login = admin_login(project, clickhouse_server, SMALLEST_ADMIN + READ_GRANTS)
    use_admin(project, login, monkeypatch)
    result = apply(project)
    assert result.exit_code == 1, result.output
    [failure] = [line for line in result.stderr.splitlines() if line.startswith("✗ replace")]
    assert f"replace the grants of role {reader} " in failure
    assert failure.endswith(
        f"necessary to have the grant SELECT ON {p}_other.* WITH GRANT OPTION. (ACCESS_DENIED)"
    ), failure
    assert read_access(project.client, [], [reader]) == before
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert [line for line in marked(planned, "-") if line.startswith("replace")] == [
        failure[2 : failure.index(": " + login.user)]
    ]


def test_a_replace_with_and_without_grant_option_converges(access_project):
    """The whole set without the option, then the option: no declared privilege is ever missing."""
    project, p, db = access_project, access_project.name, access_project.database
    mixed = f"{p}_mixed"
    grants = "['SELECT ON {db}.events', 'INSERT ON {db}.events WITH GRANT OPTION']"
    write_access(project, f"roles: {{{mixed}: {{grants: {grants}}}}}\n")
    run_all(project, f"CREATE ROLE {mixed}", f"GRANT SELECT, INSERT ON {db}.* TO {mixed}")
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 0, result.output
    sent = [sql for sql in written(project.client, project.admin, since) if f"`{mixed}`" in sql]
    assert sent == [
        f"GRANT SELECT ON `{db}`.`events`, INSERT ON `{db}`.`events` TO `{mixed}` "
        "WITH REPLACE OPTION",
        f"GRANT INSERT ON `{db}`.`events` TO `{mixed}` WITH GRANT OPTION",
    ]
    assert_plan_in_sync(project)


def test_a_replace_keeps_what_is_held_inside_a_best_effort_grant(
    access_project, clickhouse_server, monkeypatch
):
    """Best-effort `SELECT ON system.*` declared, only `system.tables` held.

    The replace keeps `system.tables`. The best-effort try after it would give it back, so
    the replace's own text is what shows this. Run as the smallest admin (U6), which holds
    some system tables with grant option, but not `system.*`.
    """
    project, p, db = access_project, access_project.name, access_project.database
    reader = f"{p}_reader"
    write_access(project, "{}\n")
    assert apply(project).exit_code == 0
    best_effort = "{grant: 'SELECT ON system.*', best_effort: true}"
    write_access(
        project, f"roles: {{{reader}: {{grants: ['SELECT ON {{db}}.events', {best_effort}]}}}}\n"
    )
    run_all(
        project,
        f"CREATE ROLE {reader}",
        f"GRANT SELECT ON {db}.* TO {reader}",
        f"GRANT SELECT ON system.tables TO {reader}",
    )
    login = admin_login(project, clickhouse_server, SMALLEST_ADMIN + READ_GRANTS)
    use_admin(project, login, monkeypatch)
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert written(project.client, login.user, since)[0] == (
        f"GRANT SELECT ON `{db}`.`events`, SELECT ON `system`.`tables` TO `{reader}` "
        "WITH REPLACE OPTION"
    )
    assert ("SELECT", "system", "tables") in held_grants(project.client, reader)
    assert result.stderr == f"! best-effort: role {reader} lacks SELECT ON system.*\n"


def test_a_revoke_inside_a_best_effort_grant_stops_a_replace(
    access_project, clickhouse_server, monkeypatch
):
    """Best-effort `SELECT ON system.*` held except for `system.users`.

    No GRANT can keep that exactly, so the narrowing replace isn't sent: it is named, and the
    role's access is unchanged. Run as the smallest admin (U6), like the sibling test above:
    it can pass on only some system tables, so the best-effort try after the skip gives
    nothing the role lacks. An admin that could pass on all of `system.*` would give
    `system.users` back through the try, as it does when no replace is pending.
    """
    project, db = access_project, access_project.database
    reader = system_reader_less_users(project)
    before = read_access(project.client, [], [reader])
    login = smallest_admin(project, clickhouse_server, monkeypatch)
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 1, result.output
    assert read_access(project.client, [], [reader]) == before, result.output
    replace_line = replacing(reader, f"revoke SELECT ON {db}.* from role {reader}")
    assert result.stderr.splitlines()[0] == (
        f"! skipped: {replace_line}, because what it holds of the best-effort grant "
        f"SELECT ON system.* has {revoked_system_tables(project, reader)} revoked inside it, "
        "which a replace can't keep, so it keeps the access it has"
    )
    assert not any("REPLACE OPTION" in sql for sql in written(project.client, login.user, since))
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert marked(planned, "-") == [replace_line]


def test_the_migration_role_is_added_to_and_never_replaced(access_project):
    """It holds `system.*`, broader than its profile's tables: an extra, never a replace (2.2)."""
    project, db, role = access_project, access_project.database, access_project.migration_role
    write_access(project, "{}\n")
    assert apply(project).exit_code == 0
    run_all(
        project,
        f"GRANT CURRENT GRANTS(SELECT ON system.*) TO {role}",
        f"REVOKE OPTIMIZE ON {db}.* FROM {role}",
    )
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert written(project.client, project.admin, since) == [
        f"GRANT OPTIMIZE ON `{db}`.* TO `{role}`"
    ]
    assert marked(result, "!") == [f"role {role} holds SELECT ON system.* beyond its profile"]
    held = project.client.query(
        "SELECT count() FROM system.grants WHERE role_name = {r:String} "
        "AND database = 'system' AND table IS NULL",
        parameters={"r": role},
    ).result_rows
    assert held == [(1,)]


def test_read_and_write_on_one_source_are_one_grant(access_project):
    """Decision 12: READ ON REMOTE declared, READ and WRITE held, as on Keeper or after a restart.

    The role is in sync and `apply` revokes nothing. The migration role, whose core profile
    has READ ON REMOTE best-effort, gets no `!` line for WRITE. `validate` warns about the
    one-direction grant, naming the setting that would keep it one direction.
    """
    project, role = access_project, f"{access_project.name}_remote"
    write_access(project, f"roles: {{{role}: {{grants: ['READ ON REMOTE']}}}}\n")
    validated = access(project, "validate", "it")
    assert validated.exit_code == 0, validated.output
    assert "access_control_improvements.enable_read_write_grants" in validated.stderr
    assert apply(project).exit_code == 0
    run_all(
        project,
        f"GRANT WRITE ON REMOTE TO {role}",
        f"GRANT WRITE ON REMOTE TO {project.migration_role}",
    )
    assert_plan_in_sync(project)
    since = server_time(project.client)
    result = apply(project)
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert result.stdout == "✓ In sync for it (access.yaml): 0 changes applied.\n"
    assert written(project.client, project.admin, since) == []
    held = project.client.query(
        "SELECT access_type FROM system.grants WHERE role_name = {r:String} "
        "AND access_object = 'REMOTE' ORDER BY access_type",
        parameters={"r": role},
    ).result_rows
    assert held == [("READ",), ("WRITE",)]


def test_a_grant_the_limited_admin_cannot_give_fails_alone(
    access_project, limited_admin, monkeypatch
):
    project, p, db = access_project, access_project.name, access_project.database
    ops = f"{p}_ops"
    write_access(
        project,
        f"roles: {{{ops}: {{grants: ['SELECT ON {{db}}.*', 'CREATE FUNCTION ON *.*']}}}}\n"
        f"users: {{{p}_u: {{password: {{secret: u_password}}, roles: [{ops}]}}}}\n",
    )
    set_secrets(project, monkeypatch, "u_password")
    result = apply(project)
    assert result.exit_code == 1, result.output
    failed = [line for line in result.stderr.splitlines() if line.startswith("✗ ")]
    assert failed[0].startswith(f"✗ grant CREATE FUNCTION ON *.* to role {ops}: "), failed
    assert failed[0].endswith("(ACCESS_DENIED)") and len(failed) == 2, failed
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert marked(planned, "+") == [f"grant CREATE FUNCTION ON *.* to role {ops}"]
    assert marked(planned, "-") == []


def test_a_best_effort_grant_the_admin_cannot_give_is_a_warning(
    access_project, limited_admin, clickhouse_server
):
    """CLUSTER only where ON CLUSTER needs the grant: ClickHouse Cloud gives it to all."""
    project, p = access_project, access_project.name
    best_effort = "{grant: CLUSTER ON *.*, best_effort: true}"
    write_access(
        project,
        f"roles: {{'{{project}}_ops': {{grants: ['SELECT ON {{db}}.*', {best_effort}]}}}}\n",
    )
    result = apply(project)
    assert result.exit_code == 0, result.output
    cluster = ["CLUSTER ON *.*"] if requires_cluster_grant(clickhouse_server) else []
    unreachable = [*cluster, "READ ON REMOTE", "CREATE TEMPORARY TABLE ON *.*"]
    assert result.stderr.splitlines() == [
        *(f"! best-effort: role {p}_migration_role lacks {grant}" for grant in unreachable),
        *(f"! best-effort: role {p}_ops lacks {grant}" for grant in cluster),
    ]
    assert result.stdout.splitlines()[-1].startswith("✓ In sync for it (access.yaml): ")


@pytest.mark.parametrize("declared", ["admin", "migration user"])
def test_the_admin_is_never_changed(access_project, declared):
    """Declaring `admin_user`, or making the migration user the admin, exits 2 before a write."""
    project, admin = access_project, access_project.admin
    if declared == "admin":
        write_access(project, f"users: {{{admin}: {{}}}}\nroles: {{{project.name}_a: {{}}}}\n")
        expected = "this is config.yaml's admin_user, which ch-migrate never changes"
    else:
        write_access(project, f"roles: {{{project.name}_a: {{}}}}\n")
        project.configure(migration_user=admin)
        expected = f"✗ The migration user {admin} is config.yaml's admin_user"
    before = show_access(project)
    result = apply(project)
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert expected in result.stderr, result.stderr
    assert show_access(project) == before


def test_an_unset_secret_exits_2_before_any_change(access_project, monkeypatch):
    project = access_project
    write_access(project, ACCESS_YAML)
    set_secrets(project, monkeypatch, "dashboards_password")
    monkeypatch.delenv("CH_IT_ETL_PASSWORD", raising=False)
    before = show_access(project)
    result = apply(project)
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr.startswith("✗ CH_IT_ETL_PASSWORD is required."), result.stderr
    assert show_access(project) == before


def test_a_new_user_with_no_password_exits_2_before_any_change(access_project):
    project, p = access_project, access_project.name
    write_access(project, f"roles: {{{p}_a: {{}}}}\nusers: {{{p}_u: {{roles: [{p}_a]}}}}\n")
    before = show_access(project)
    result = apply(project)
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr == (
        f"✗ user {p}_u isn't on the server and has no password, so it can't be created. "
        "Set `password: {secret: NAME}`, or create the user first.\n"
    )
    assert show_access(project) == before


def test_an_existing_user_with_no_password_is_managed_as_declared(access_project):
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, f"users: {{'{{project}}_u': {{grants: ['SELECT ON {{db}}.*']}}}}\n")
    project.client.command(f"CREATE USER {p}_u IDENTIFIED BY '{project.new_password()}'")
    project.client.command(f"GRANT INSERT ON {db}.* TO {p}_u")
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert [line for line in marked(result, "+") if line.endswith(f"user {p}_u")] == [
        f"grant SELECT ON {db}.* to user {p}_u"
    ]
    assert marked(result, "-") == [f"revoke INSERT ON {db}.* from user {p}_u"]
    assert existing(project.client, "users", f"{p}_u") == [(f"{p}_u",)]
    assert_plan_in_sync(project)


def test_no_revoke_without_its_replacement(access_project, clickhouse_server, monkeypatch):
    """A user moves from role A to role B, and the admin can't grant B: the user keeps A."""
    project, p, db = access_project, access_project.name, access_project.database
    role_a, role_b, user = f"{p}_a", f"{p}_b", f"{p}_u"
    password = user_holding(project, monkeypatch, role_a)
    # This admin can read access and grant A, and nothing else.
    login = admin_login(project, clickhouse_server, READ_GRANTS)
    project.client.command(f"GRANT {role_a} TO {login.user} WITH ADMIN OPTION")
    use_admin(project, login, monkeypatch)
    write_access(project, holding(project, role_b))
    result = apply(project)
    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines()[:2] == [
        f"✗ grant role {role_b} to user {user}: Not enough privileges. To execute this query, "
        f"it's necessary to have the role {role_b} granted with ADMIN option. (ACCESS_DENIED)",
        f"! skipped: revoke role {role_a} from user {user}, because a change to user {user} "
        "failed, so it keeps the access it has",
    ]
    assert memberships(project.client, user) == [role_a]
    assert reads(replace(clickhouse_server, user=user, password=password), f"{db}.events")
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert marked(planned, "+") == [f"grant role {role_b} to user {user}"]
    assert marked(planned, "-") == [f"revoke role {role_a} from user {user}"]


def test_a_failed_role_protects_its_holders(access_project, clickhouse_server, monkeypatch):
    """Design 2.5: the user moves from role A to role B. `apply` grants B to the user,
    but B's own grant fails, so the revoke of A is skipped and the user still reads through A.
    """
    project, p, db = access_project, access_project.name, access_project.database
    role_a, role_b, user = f"{p}_a", f"{p}_b", f"{p}_u"
    password = user_holding(project, monkeypatch, role_a)
    project.client.command(f"REVOKE SELECT ON {db}.regions FROM {role_b}")
    # This admin can read access and grant both roles, but can't give B's grant.
    login = admin_login(project, clickhouse_server, READ_GRANTS)
    for role in (role_a, role_b):
        project.client.command(f"GRANT {role} TO {login.user} WITH ADMIN OPTION")
    use_admin(project, login, monkeypatch)
    write_access(project, holding(project, role_b))
    result = apply(project)
    assert result.exit_code == 1, result.output
    skipped = denied_then_skipped(result, f"grant SELECT ON {db}.regions to role {role_b}")
    assert skipped == (
        f"! skipped: revoke role {role_a} from user {user}, because a change to role {role_b}, "
        "which it holds, failed, so it keeps the access it has"
    )
    assert memberships(project.client, user) == [role_a, role_b]
    assert reads(replace(clickhouse_server, user=user, password=password), f"{db}.events")
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert marked(planned, "+") == [f"grant SELECT ON {db}.regions to role {role_b}"]
    assert marked(planned, "-") == [f"revoke role {role_a} from user {user}"]


def test_a_replace_waits_for_the_roles_it_holds(access_project, clickhouse_server, monkeypatch):
    """Design 2.5: a replace is gated like a revoke. `base`, a role the reader holds,
    fails its grant, so the reader's replace is skipped and named, and it keeps what it holds."""
    project, p, db = access_project, access_project.name, access_project.database
    reader, base = narrowed_reader(project), f"{p}_base"
    write_access(
        project,
        f"roles:\n  {base}: {{grants: ['SELECT ON {{db}}.regions', 'CREATE FUNCTION ON *.*']}}\n"
        f"  {reader}: {{grants: ['SELECT ON {{db}}.events'], roles: [{base}]}}\n",
    )
    before = read_access(project.client, [], [reader])
    login = admin_login(project, clickhouse_server, SMALLEST_ADMIN + READ_GRANTS)
    use_admin(project, login, monkeypatch)
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 1, result.output
    replacing = (
        f"replace the grants of role {reader} with its declared set in one statement, "
        f"instead of: revoke SELECT ON {db}.* from role {reader}"
    )
    skipped = denied_then_skipped(result, f"grant CREATE FUNCTION ON *.* to role {base}")
    assert skipped == (
        f"! skipped: {replacing}, because a change to role {base}, which it holds, failed, "
        "so it keeps the access it has"
    )
    assert not any("REPLACE OPTION" in sql for sql in written(project.client, login.user, since))
    assert read_access(project.client, [], [reader]) == before
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert marked(planned, "+") == [f"grant CREATE FUNCTION ON *.* to role {base}"]
    assert marked(planned, "-") == [replacing]


def test_a_failed_role_keeps_the_default_roles(access_project, clickhouse_server, monkeypatch):
    """The user moves from role A to role B, its new default role, and B's
    own grant fails. Its default roles don't change, so a fresh login still reads through A."""
    project, p, db = access_project, access_project.name, access_project.database
    role_a, role_b, user = f"{p}_a", f"{p}_b", f"{p}_u"
    password = user_holding(project, monkeypatch, role_a)
    run_all(
        project,
        f"ALTER USER {user} DEFAULT ROLE {role_a}",
        f"REVOKE SELECT ON {db}.regions FROM {role_b}",
    )
    # This admin can read access, grant both roles and set u's default roles, but can't give
    # B's grant. Ungated, the default-role change would land. ALTER USER is on u alone:
    # ClickHouse Cloud's admin can't give it on every user.
    login = admin_login(project, clickhouse_server, (*READ_GRANTS, f"ALTER USER ON {user}"))
    for role in (role_a, role_b):
        project.client.command(f"GRANT {role} TO {login.user} WITH ADMIN OPTION")
    use_admin(project, login, monkeypatch)
    moved = holding(project, role_b)
    write_access(project, moved.replace(f"[{role_b}]", f"[{role_b}], default_roles: [{role_b}]"))
    result = apply(project)
    assert result.exit_code == 1, result.output
    assert default_roles(project.client, user) == [role_a], result.output
    assert reads(replace(clickhouse_server, user=user, password=password), f"{db}.events")
    skipped = denied_then_skipped(result, f"grant SELECT ON {db}.regions to role {role_b}")
    assert skipped == (
        f"! skipped: set the default roles of user {user} to {role_b}, because a change to role "
        f"{role_b}, which it holds, failed, so it keeps the access it has"
    )
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert f"set the default roles of user {user} to {role_b}" in marked(planned, "+")


def test_a_role_is_replaced_before_the_role_that_holds_it(
    access_project, clickhouse_server, monkeypatch
):
    """The reader is declared before `base`, the role it holds, and both
    narrow a scope. Base also holds a right the admin can't revoke, so its replace fails. It
    runs first anyway, and the reader's replace is skipped and named: the reader keeps its
    access. Run as the smallest admin (U6) plus the grants it needs to read access."""
    project, p, db = access_project, access_project.name, access_project.database
    reader, base = holder_declared_first(project)
    before = read_access(project.client, [], [reader])
    login = smallest_admin(project, clickhouse_server, monkeypatch)
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 1, result.output
    assert read_access(project.client, [], [reader]) == before, result.output
    revokes = [f"revoke SELECT ON {on} from role {base}" for on in (f"{db}.*", f"{p}_other.*")]
    skipped = denied_then_skipped(result, replacing(base, *revokes))
    reader_replace = replacing(reader, f"revoke SELECT ON {db}.* from role {reader}")
    assert skipped == (
        f"! skipped: {reader_replace}, because a change to role {base}, which it holds, failed, "
        "so it keeps the access it has"
    )
    sent = written(project.client, login.user, since)
    assert not any(f"TO `{reader}` WITH REPLACE OPTION" in sql for sql in sent), sent
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert reader_replace in marked(planned, "-")


def test_a_held_back_replace_blocks_drops(access_project, clickhouse_server, monkeypatch):
    """The user moves from role `old`, declared `absent: true`, to
    `reader`, whose replace can't keep the revoke inside its best-effort grant. Nothing fails,
    but the revoke of `old` and `DROP ROLE old` are both skipped and named, and the user still
    reads through `old`. The admin may drop roles, so only the gate keeps `old`."""
    project, p, db = access_project, access_project.name, access_project.database
    old, reader, user = f"{p}_old", f"{p}_reader", f"{p}_u"
    password = user_leaving_old(project, monkeypatch)
    grants = (*SMALLEST_ADMIN, *READ_GRANTS, "DROP ROLE ON *.*")
    login = admin_login(project, clickhouse_server, grants)
    use_admin(project, login, monkeypatch)
    since = server_time(project.client)
    result = apply(project)
    assert result.exit_code == 1, result.output
    assert existing(project.client, "roles", old) == [(old,)], result.output
    assert memberships(project.client, user) == [old, reader]
    assert reads(replace(clickhouse_server, user=user, password=password), f"{db}.events")
    sent = written(project.client, login.user, since)
    assert not any(sql.startswith(("DROP", "REVOKE")) for sql in sent), sent
    revoke = f"revoke role {old} from user {user}"
    skipped = [line for line in result.stderr.splitlines() if line.startswith("! skipped: ")]
    assert skipped[0].startswith(f"! skipped: replace the grants of role {reader} "), skipped
    assert skipped[1:] == [
        f"! skipped: {revoke}, because a change to role {reader}, which it holds, couldn't be "
        "sent, so it keeps the access it has",
        f"! skipped: drop role {old}, because the replace of role {reader} couldn't be sent",
    ]
    planned = access(project, "plan", "it")
    assert planned.exit_code == 1, planned.output
    assert marked(planned, "-")[1:] == [revoke, f"drop role {old}"]


def test_passwords_are_set_once_and_reset_on_request(
    access_project, clickhouse_server, monkeypatch
):
    project, p = access_project, access_project.name
    user = f"{p}_u"
    write_access(project, f"users: {{{user}: {{password: {{secret: u_password}}}}}}\n")
    [first] = set_secrets(project, monkeypatch, "u_password").values()
    outputs = [apply(project)]
    assert outputs[-1].exit_code == 0, outputs[-1].output
    assert logs_in(clickhouse_server, user, first)
    [second] = set_secrets(project, monkeypatch, "u_password").values()
    outputs.append(apply(project))
    assert outputs[-1].stdout == "✓ In sync for it (access.yaml): 0 changes applied.\n"
    assert logs_in(clickhouse_server, user, first) and not logs_in(clickhouse_server, user, second)
    outputs.append(apply(project, "--reset-passwords"))
    assert outputs[-1].exit_code == 0, outputs[-1].output
    assert outputs[-1].stdout.splitlines() == [
        f"+ reset the password of user {user}",
        "✓ In sync for it (access.yaml): 1 change applied.",
    ]
    assert logs_in(clickhouse_server, user, second) and not logs_in(clickhouse_server, user, first)
    for output in outputs:
        assert_no_secret(output, [first, second, *migration_password(monkeypatch)])


def test_every_on_cluster_form_parses_with_a_hyphenated_cluster_name(access_project):
    """Quoted, a cluster name such as `c-1` parses in every statement `apply`
    can send ON CLUSTER. The server only formats each one, so none of them runs."""
    project = access_project
    reader = f"{project.name}_reader"
    write_access(project, f"roles: {{{reader}: {{grants: ['SELECT ON {{db}}.events']}}}}\n")
    spec = validate_access(load_settings("it", project.root / "config.yaml"), project.root).spec
    writing = Writing(spec, LiveState(bundled_tree()), "c-1", {"u": "secret"})
    sql = [write.sql for change in every_form(reader) for write in writes_for(change, writing)]
    assert len(sql) == 15
    for text in sql:
        formatted = project.client.command("SELECT formatQuery({q:String})", parameters={"q": text})
        assert "ON CLUSTER `c-1`" in formatted, (text, formatted)


@pytest.mark.cluster
def test_cluster_local_storage_runs_on_cluster_and_reaches_every_node(
    clickhouse_cluster, monkeypatch, request
):
    cluster = clickhouse_cluster
    project = cluster_project(cluster, request)
    write_access(project, ACCESS_YAML)
    passwords = set_secrets(project, monkeypatch, *SECRETS)
    sent = record_writes(monkeypatch)
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert sent and all(f" ON CLUSTER `{cluster.name}`" in sql for sql, _ in sent), sent
    # wait_end_of_query is an HTTP parameter: query_log doesn't keep it, so it is read here.
    assert all(settings.get("wait_end_of_query") == 1 for _, settings in sent), sent
    # Decision 12: each best-effort privilege is a plain GRANT, never CURRENT GRANTS.
    assert not any("CURRENT GRANTS" in sql for sql, _ in sent), sent
    role, db = project.migration_role, project.database
    every_best_effort = [
        ("CLUSTER", None, None),
        ("CREATE TEMPORARY TABLE", None, None),
        ("READ", None, None),
        ("SELECT", "system", "parts"),
        ("SELECT", "system", "replicas"),
        ("SYSTEM SYNC DATABASE REPLICA", db, None),
        ("SYSTEM SYNC REPLICA", db, None),
    ]
    for node in (1, 2):
        assert best_effort_held(cluster.clients[node], role, db) == every_best_effort, node
        project.configure(port=cluster.servers[node].port)
        assert_plan_in_sync(project, cluster.clients[node])
        server = cluster.servers[node]
        assert logs_in(server, f"{project.name}_etl", passwords["etl_password"]), node
    # Its admin holds every best-effort grant, so none is left to warn about.
    assert result.stderr == "", result.output


@pytest.mark.cluster
def test_cluster_local_storage_best_effort_lands_what_a_partial_admin_holds(
    clickhouse_cluster, monkeypatch, request
):
    """Decision 12: each best-effort privilege is its own plain GRANT ON CLUSTER.

    The three the admin can pass on reach every node. Each other one is refused, with a
    warning that names it, and `apply` exits 0.
    """
    cluster = clickhouse_cluster
    project = cluster_project(cluster, request)
    role, db = project.migration_role, project.database
    write_access(project, "{}\n")
    login = cluster_admin(cluster, project, PARTIAL_ADMIN)
    use_admin(project, login, monkeypatch)
    sent = record_writes(monkeypatch)
    result = apply(project)
    assert result.exit_code == 0, result.output
    assert not any("CURRENT GRANTS" in sql for sql, _ in sent), sent
    assert all(f" ON CLUSTER `{cluster.name}`" in sql for sql, _ in sent), sent
    assert all(settings.get("wait_end_of_query") == 1 for _, settings in sent), sent
    refused = [
        "SELECT ON system.replicas",
        f"SYSTEM SYNC DATABASE REPLICA ON {db}.*",
        "CLUSTER ON *.*",
        "READ ON REMOTE",
    ]
    assert_each_refusal_warns(result, role, refused)
    for node in (1, 2):
        assert best_effort_held(cluster.clients[node], role, db) == [
            ("CREATE TEMPORARY TABLE", None, None),
            ("SELECT", "system", "parts"),
            ("SYSTEM SYNC REPLICA", db, None),
        ], node
        project.configure(port=cluster.servers[node].port)
        assert_plan_in_sync(project, cluster.clients[node])


@pytest.mark.cluster
def test_cluster_keeper_storage_needs_no_on_cluster(
    clickhouse_cluster_keeper, monkeypatch, request
):
    cluster = clickhouse_cluster_keeper
    project = cluster_project(cluster, request)
    write_access(project, ACCESS_YAML)
    passwords = set_secrets(project, monkeypatch, *SECRETS)
    since = server_time(cluster.clients[1])
    result = apply(project)
    assert result.exit_code == 0, result.output
    # Every replica is read at once, before and after the writes, so nothing waits (T8).
    assert "Reading again" not in result.stderr, result.stderr
    sent = written(cluster.clients[1], project.admin, since)
    assert sent and not any("ON CLUSTER" in sql for sql in sent), sent
    project.configure(port=cluster.servers[2].port, access_read_timeout=30)
    planned = access(project, "plan", "it")
    assert planned.exit_code == 0, planned.output
    # 26.3 keeps `READ ON REMOTE` in Keeper as `REMOTE ON *.*`, which reads back as READ and
    # WRITE. Since Decision 12 they compare as one grant, so the migration role has no `!` line.
    assert (planned.stdout, planned.stderr) == (PLAN_IN_SYNC, ""), planned.output
    assert logs_in(cluster.servers[2], f"{project.name}_etl", passwords["etl_password"])


@pytest.mark.cluster
def test_an_on_cluster_error_after_a_host_answered_keeps_its_name(
    clickhouse_cluster, monkeypatch, request
):
    """Node 2 starts late and fails the grant, after node 1 has answered (T2, U9).

    The role is made on node 1 only, so the grant's first host succeeds and node 2 fails.
    Without `wait_end_of_query=1`, the client would see a broken stream with no code.
    """
    cluster = clickhouse_cluster
    project = cluster_project(cluster, request)
    late = f"{project.name}_late"
    write_access(project, "{}\n")
    assert apply(project).exit_code == 0
    write_access(project, f"roles: {{{late}: {{grants: ['SELECT ON {{db}}.*']}}}}\n")
    cluster.clients[1].command(f"CREATE ROLE {late}")
    result = apply_with_node_2_late(cluster, project)
    assert result.exit_code == 1, result.output
    [failure] = [line for line in result.stderr.splitlines() if line.startswith("✗ grant")]
    assert failure.startswith(f"✗ grant SELECT ON {project.database}.* to role {late}: ")
    assert failure.endswith("(UNKNOWN_ROLE)"), failure
    assert "Connection broken" not in result.output and "IncompleteRead" not in result.output


def apply(project, *args):
    """Run `access apply it`. It may exit 1, but never because it stopped part-way."""
    result = access(project, "apply", "it", *args)
    assert "stopped part-way" not in result.stderr, result.output
    return result


def assert_plan_in_sync(project, node=None):
    """`plan` exits 0 with nothing to change. On a local-storage cluster, it first names the
    node it read: the one `node`, a client, is connected to (Decision 14)."""
    result = access(project, "plan", "it")
    named = "" if node is None else node_line(node) + "\n"
    assert (result.exit_code, result.stdout) == (0, named + PLAN_IN_SYNC), result.output


def node_line(client):
    """What `plan`, `apply` and `bootstrap` print on a local-storage cluster, read from the
    node `client` is connected to (Decision 14)."""
    node = client.command("SELECT hostName()")
    return (
        f"→ Read node {node} only: users and roles are on each node's own disk. After a failed "
        "ON CLUSTER apply, run ch-migrate access plan it against each node."
    )


def assert_each_refusal_warns(result, role, refused):
    """Each best-effort grant the admin refused warns as `apply` tries it, then as a shortfall."""
    warnings = result.stderr.splitlines()
    assert len(warnings) == 2 * len(refused), result.stderr
    for line, grant in zip(warnings, refused):
        tried = f"! grant role {role} as much of {grant} as the admin holds (best-effort): "
        assert line.startswith(tried) and ": Not enough privileges." in line, line
        assert line.endswith(f"have the grant {grant} WITH GRANT OPTION. (ACCESS_DENIED)"), line
    lacks = [f"! best-effort: role {role} lacks {grant}" for grant in refused]
    assert warnings[len(refused) :] == lacks


def denied_then_skipped(result, change):
    """The second line `apply` warns with, once the first says the admin was refused `change`."""
    failed, skipped = result.stderr.splitlines()[:2]
    assert failed.startswith(f"✗ {change}: ") and failed.endswith("(ACCESS_DENIED)"), failed
    return skipped


def assert_no_secret(result, secrets):
    assert not any(secret in result.output for secret in secrets), "a secret was printed"


def set_secrets(project, monkeypatch, *names):
    """A new value for each secret, in `CH_IT_<NAME>`; returns them by name."""
    values = {name: project.new_password() for name in names}
    for name, value in values.items():
        monkeypatch.setenv(f"CH_IT_{name.upper()}", value)
    return values


def migration_password(monkeypatch):
    return [os.environ["CH_IT_MIGRATION_PASSWORD"]]


def admin_login(project, server, grants):
    """A login named `<project>_admin` that holds only `grants`, `{db}` filled in."""
    login = replace(server, user=f"{project.name}_admin", password=project.new_password())
    project.client.command(
        f"CREATE USER {login.user} IDENTIFIED BY {{password:String}}",
        parameters={"password": login.password},
    )
    for grant in grants:
        text = grant.format(db=project.database)
        option = text.endswith(" WITH GRANT OPTION")
        what = text.removesuffix(" WITH GRANT OPTION")
        suffix = " WITH GRANT OPTION" if option else ""
        project.client.command(f"GRANT {what} TO {login.user}{suffix}")
    return login


def cluster_admin(cluster, project, grants):
    """`admin_login` on every node of a cluster whose users are on each node's own disk."""
    login = replace(
        cluster.servers[1], user=f"{project.name}_admin", password=project.new_password()
    )
    client, on_cluster = cluster.clients[1], f"ON CLUSTER {cluster.name}"
    client.command(
        f"CREATE USER {login.user} {on_cluster} IDENTIFIED BY {{password:String}}",
        parameters={"password": login.password},
    )
    for grant in grants:
        text = grant.format(db=project.database)
        what = text.removesuffix(" WITH GRANT OPTION")
        suffix = text[len(what) :]
        client.command(f"GRANT {on_cluster} {what} TO {login.user}{suffix}")
    return login


def use_admin(project, login, monkeypatch):
    """Make `login` config.yaml's admin_user."""
    project.configure(admin_user=login.user)
    monkeypatch.setenv("CH_IT_ADMIN_PASSWORD", login.password)


def narrowed_reader(project):
    """A role declared with `SELECT ON {db}.events` and role `base`, holding `{db}.*` instead.

    Everything else is applied first, by the server's admin.
    """
    p, db = project.name, project.database
    reader = f"{p}_reader"
    write_access(
        project,
        f"roles:\n  {p}_base: {{grants: ['SELECT ON {{db}}.regions']}}\n"
        f"  {reader}: {{grants: ['SELECT ON {{db}}.events'], roles: [{p}_base]}}\n",
    )
    assert apply(project).exit_code == 0
    run_all(
        project,
        f"REVOKE SELECT ON {db}.events FROM {reader}",
        f"GRANT SELECT ON {db}.* TO {reader}",
    )
    return reader


def holder_declared_first(project):
    """Role `reader`, declared before `base`, the role it holds; each holds `SELECT ON {db}.*`
    instead of its declared table, and base also holds `SELECT ON <project>_other.*`.

    Everything else is applied first, by the server's admin. Returns reader and base.
    """
    p, db = project.name, project.database
    reader, base = f"{p}_reader", f"{p}_base"
    write_access(
        project,
        f"roles:\n  {reader}: {{grants: ['SELECT ON {{db}}.events'], roles: [{base}]}}\n"
        f"  {base}: {{grants: ['SELECT ON {{db}}.regions']}}\n",
    )
    assert apply(project).exit_code == 0
    run_all(
        project,
        f"REVOKE SELECT ON {db}.events FROM {reader}",
        f"GRANT SELECT ON {db}.* TO {reader}",
        f"REVOKE SELECT ON {db}.regions FROM {base}",
        f"GRANT SELECT ON {db}.* TO {base}",
        f"GRANT SELECT ON {p}_other.* TO {base}",
    )
    return reader, base


def system_reader_less_users(project):
    """Role `reader`, declared with `SELECT ON {db}.events` and best-effort
    `SELECT ON system.*`, holding `SELECT ON {db}.*` and `system.*` less `system.users`: as
    much of `system.*` as the server's admin holds.

    The migration role and user are applied first, by the server's admin.
    """
    db, reader = project.database, f"{project.name}_reader"
    write_access(project, "{}\n")
    assert apply(project).exit_code == 0
    best_effort = "{grant: 'SELECT ON system.*', best_effort: true}"
    write_access(
        project, f"roles: {{{reader}: {{grants: ['SELECT ON {{db}}.events', {best_effort}]}}}}\n"
    )
    run_all(
        project,
        f"CREATE ROLE {reader}",
        f"GRANT SELECT ON {db}.* TO {reader}",
        f"GRANT CURRENT GRANTS(SELECT ON system.*) TO {reader}",
        f"REVOKE SELECT ON system.users FROM {reader}",
    )
    return reader


def revoked_system_tables(project, role):
    """`SELECT ON system.users`, and any other system table revoked inside `role`'s
    `SELECT ON system.*`: on ClickHouse Cloud, whose admin can't give it,
    `system.zookeeper`."""
    rows = project.client.query(
        "SELECT table FROM system.grants WHERE role_name = {r:String} AND is_partial_revoke "
        "AND access_type = 'SELECT' AND database = 'system' ORDER BY table",
        parameters={"r": role},
    ).result_rows
    return ", ".join(f"SELECT ON system.{table}" for (table,) in rows)


def user_leaving_old(project, monkeypatch):
    """User `u`, holding role `old`, which alone gives `SELECT ON {db}.events`, is declared to
    move to role `reader`, and `old` to be `absent`. Reader holds `INSERT ON {db}.*` instead
    of its declared table, and best-effort `SELECT ON system.*` less `system.users`.

    Everything is made by the server's admin. Returns u's password.
    """
    p, db = project.name, project.database
    old, reader, user = f"{p}_old", f"{p}_reader", f"{p}_u"
    [password] = set_secrets(project, monkeypatch, "u_password").values()
    users = f"users: {{{user}: {{password: {{secret: u_password}}, roles: [ROLE]}}}}\n"
    write_access(
        project,
        f"roles: {{{old}: {{grants: ['SELECT ON {{db}}.events']}}}}\n" + users.replace("ROLE", old),
    )
    run_all(
        project,
        f"CREATE DATABASE {db}",
        f"CREATE TABLE {db}.events (id UInt64) ENGINE = MergeTree ORDER BY id",
    )
    assert apply(project).exit_code == 0
    run_all(
        project,
        f"CREATE ROLE {reader}",
        f"GRANT INSERT ON {db}.* TO {reader}",
        f"GRANT CURRENT GRANTS(SELECT ON system.*) TO {reader}",
        f"REVOKE SELECT ON system.users FROM {reader}",
    )
    best_effort = "{grant: 'SELECT ON system.*', best_effort: true}"
    reading = f"{reader}: {{grants: ['INSERT ON {{db}}.events', {best_effort}]}}"
    write_access(
        project, f"roles: {{{old}: {{absent: true}}, {reading}}}\n" + users.replace("ROLE", reader)
    )
    return password


def smallest_admin(project, server, monkeypatch):
    """Make the smallest admin (U6), plus the grants it reads access with, `apply`'s login."""
    login = admin_login(project, server, SMALLEST_ADMIN + READ_GRANTS)
    use_admin(project, login, monkeypatch)
    return login


def replacing(role, *revokes):
    """The plan's line for a replace of `role` that stands in for these revokes."""
    return (
        f"replace the grants of role {role} with its declared set in one statement, "
        f"instead of: {'; '.join(revokes)}"
    )


def holding(project, role):
    """Roles `a` and `b`, and user `u` that holds `role`."""
    p = project.name
    return (
        f"roles: {{{p}_a: {{grants: ['SELECT ON {{db}}.events']}}, "
        f"{p}_b: {{grants: ['SELECT ON {{db}}.regions']}}}}\n"
        f"users: {{{p}_u: {{password: {{secret: u_password}}, roles: [{role}]}}}}\n"
    )


def user_holding(project, monkeypatch, role):
    """`holding(role)` applied by the server's admin, and table `{db}.events`; u's password."""
    [password] = set_secrets(project, monkeypatch, "u_password").values()
    db = project.database
    run_all(
        project,
        f"CREATE DATABASE {db}",
        f"CREATE TABLE {db}.events (id UInt64) ENGINE = MergeTree ORDER BY id",
    )
    write_access(project, holding(project, role))
    assert apply(project).exit_code == 0
    return password


def reads(login, table):
    """Whether `login` can read the empty `table`."""
    client = login.connect()
    try:
        return client.command(f"SELECT count() FROM {table}") == 0
    finally:
        client.close()


def server_time(client):
    """The server's clock now: query_log rows after it are this test's."""
    return client.command("SELECT toString(now64(6))")


def written(client, user, since):
    """What `user` sent since `since` that isn't a read, in order, from query_log.

    A read is a SELECT or a CHECK GRANT. Every replica's log is read (`query_log`), and the
    flush that makes it readable isn't counted.
    """
    rows = client.query(
        f"SELECT query FROM {query_log(client)} "
        "WHERE type = 'QueryStart' AND is_initial_query AND user = {user:String} "
        "AND query_kind != 'Select' AND NOT startsWith(query, 'CHECK GRANT') "
        "AND NOT startsWith(query, 'SYSTEM FLUSH LOGS') "
        "AND event_time_microseconds > {since:DateTime64(6)} "
        "ORDER BY event_time_microseconds",
        parameters={"user": user, "since": since},
    ).result_rows
    return [query for (query,) in rows]


def record_writes(monkeypatch):
    """Record each statement `apply` sends, with the settings it sends it with."""
    from ch_migrate.access import cli

    sent = []
    connect = cli.connect_admin

    def recording(settings):
        client = connect(settings)
        command = client.command

        def recorded(sql, parameters=None, settings=None):
            if not sql.startswith("CHECK GRANT"):  # a read, as of whether CLUSTER is everyone's
                sent.append((sql, dict(settings or {})))
            return command(sql, parameters=parameters, settings=settings)

        client.command = recorded
        return client

    monkeypatch.setattr(cli, "connect_admin", recording)
    return sent


def existing(client, table, name):
    return client.query(
        f"SELECT name FROM system.{table} WHERE name = {{n:String}}", parameters={"n": name}
    ).result_rows


def memberships(client, grantee):
    rows = client.query(
        "SELECT granted_role_name FROM system.role_grants "
        "WHERE user_name = {g:String} OR role_name = {g:String} ORDER BY granted_role_name",
        parameters={"g": grantee},
    ).result_rows
    return [name for (name,) in rows]


def default_roles(client, user):
    """The roles `user` logs in with, when it doesn't log in with all of them."""
    [(roles,)] = client.query(
        "SELECT default_roles_list FROM system.users WHERE name = {u:String}",
        parameters={"u": user},
    ).result_rows
    return sorted(roles)


def logs_in(server, user, password):
    try:
        client = replace(server, user=user, password=password).connect()
    except DatabaseError:
        return False
    try:
        return client.command("SELECT currentUser()") == user
    finally:
        client.close()


def held_grants(client, role):
    """Each `system.grants` row of `role`: its privilege, database and table."""
    rows = client.query(
        "SELECT toString(access_type), database, table FROM system.grants "
        "WHERE role_name = {r:String}",
        parameters={"r": role},
    ).result_rows
    return [tuple(row) for row in rows]


def best_effort_held(client, role, db):
    """The rows of core's best-effort grants that `role` holds, as one node reads them."""
    rows = held_grants(client, role)
    best_effort = {
        ("SELECT", "system", "parts"),
        ("SELECT", "system", "replicas"),
        ("SYSTEM SYNC REPLICA", db, None),
        ("SYSTEM SYNC DATABASE REPLICA", db, None),
        ("CLUSTER", None, None),
        ("READ", None, None),
        ("CREATE TEMPORARY TABLE", None, None),
    }
    return sorted((tuple(row) for row in rows if tuple(row) in best_effort), key=str)


def every_form(role):
    """One change for each statement in `sql/apply.sql` that can run ON CLUSTER."""
    reader, user = Entity("role", role), Entity("user", "u")
    select = Grant(("SELECT",), Target("db", "t"))
    optioned = Grant(("SELECT",), Target("db", "t"), grant_option=True)
    return [
        Change("create_role", reader),
        Change("create_user", user),
        Change("reset_password", user),
        Change("grant_role", user, roles=(role,)),
        Change("grant", reader, grant=select),
        Change("grant", reader, grant=optioned),
        Change("replace", reader),
        Change("best_effort", reader, grant=optioned),
        Change("set_default_roles", user, roles=(role,)),
        Change("revoke", reader, grant=select),
        Change("revoke_grant_option", reader, grant=select),
        Change("revoke_role", user, roles=(role,)),
        Change("revoke_admin_option", user, roles=(role,)),
        Change("drop_user", user),
        Change("drop_role", reader),
    ]


def cluster_project(cluster, request):
    """A project on node 1 with `cluster` set; its users and roles dropped on every node."""
    tmp_path = request.getfixturevalue("tmp_path")
    monkeypatch = request.getfixturevalue("monkeypatch")
    name = "it_" + secrets.token_hex(8)
    project = AccessProject(tmp_path, cluster.clients[1], name)
    request.addfinalizer(lambda: drop_everywhere(cluster, name))
    initialize_cluster_project(cluster, project, monkeypatch)
    monkeypatch.setenv("CH_IT_MIGRATION_PASSWORD", project.new_password())
    return project


def drop_everywhere(cluster, prefix):
    for node in sorted(cluster.stopped):
        cluster.start_node(node)
    for client in cluster.clients.values():
        for kind, table in (("USER", "system.users"), ("ROLE", "system.roles")):
            rows = client.query(
                f"SELECT name FROM {table} WHERE startsWith(name, {{p:String}})",
                parameters={"p": prefix},
            ).result_rows
            for (name,) in rows:
                client.command(f"DROP {kind} IF EXISTS `{name}`")


def apply_with_node_2_late(cluster, project):
    """Run `apply` from node 1 while node 2 is down, and start node 2 three seconds later."""
    results = []
    applying = threading.Thread(target=lambda: results.append(apply(project)))
    cluster.stop_node(2)
    try:
        applying.start()
        time.sleep(3)
    finally:
        cluster.start_node(2)
        applying.join(120)
    assert results, "apply did not finish within 120 seconds"
    return results[0]
