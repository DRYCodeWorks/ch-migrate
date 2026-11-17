"""`access show` and `access plan` against a real server (design note, 2.5 and 2.7).

Every user and role a test creates starts with the project's `it_<hex>` name, so the access
fixtures' finalizer drops them. `plan` and `show` only read. So each test makes the server
differ by hand, as its admin, and asserts what the commands print and how they exit.

Exit 1 means "changes pending", and a crash would exit 1 too. So `access()` fails the test
on any exception, and every exit-1 assertion comes with the lines it expects.
"""

import re
import secrets
import socket
from pathlib import Path

import pytest
import yaml
from access_fixtures import AccessProject, initialize_cluster_project, requires_cluster_grant
from clickhouse_connect.driver.exceptions import DatabaseError
from grant_rights import PrivilegeTree, gives_in_full

from ch_migrate.access import profiles
from ch_migrate.access.live import LOCAL_STORAGE, REPLICATED_STORAGE, Wanted, read_live
from ch_migrate.sql import split_statements

pytestmark = pytest.mark.integration

ACCESS_YAML = """\
migration_role:
  grants:
    - CREATE FUNCTION, DROP FUNCTION ON *.*
roles:
  '{project}_analyst':
    grants:
      - SELECT, SHOW TABLES ON {db}.*
  '{project}_loader':
    grants:
      - INSERT, SELECT ON {db}.events
  '{project}_reporting':
    roles: ['{project}_analyst']
users:
  '{project}_dashboards':
    password: {secret: dashboards_password}
    roles: ['{project}_reporting']
  '{project}_etl':
    password: {secret: etl_password}
    roles: ['{project}_loader', '{project}_analyst']
    default_roles: ['{project}_loader']
    grants:
      - SELECT ON {db}.regions
"""
# What `plan` prints for ACCESS_YAML on a server that holds none of it, in apply's order.
NOTHING_APPLIED = """\
create role {p}_migration_role
create role {p}_analyst
create role {p}_loader
create role {p}_reporting
create user {p}_migration
create user {p}_dashboards
create user {p}_etl
grant role {p}_migration_role to user {p}_migration
grant role {p}_analyst to role {p}_reporting
grant role {p}_reporting to user {p}_dashboards
grant role {p}_loader to user {p}_etl
grant role {p}_analyst to user {p}_etl
grant ALTER, CREATE TABLE, DROP TABLE, UNDROP TABLE ON {p}_db.* to role {p}_migration_role
grant CREATE DICTIONARY, CREATE VIEW, DROP DICTIONARY, DROP VIEW ON {p}_db.* to role {p}_migration_role
grant ALTER DELETE, INSERT, OPTIMIZE, SELECT, TRUNCATE ON {p}_db.* to role {p}_migration_role
grant SHOW COLUMNS, SHOW DICTIONARIES, SHOW TABLES ON {p}_db.* to role {p}_migration_role
grant SELECT ON system.databases to role {p}_migration_role
grant SELECT ON system.tables to role {p}_migration_role
grant SELECT ON system.mutations to role {p}_migration_role
grant SELECT ON system.dictionaries to role {p}_migration_role
grant CREATE FUNCTION, DROP FUNCTION ON *.* to role {p}_migration_role
grant SELECT, SHOW TABLES ON {p}_db.* to role {p}_analyst
grant INSERT, SELECT ON {p}_db.events to role {p}_loader
grant SELECT ON {p}_db.regions to user {p}_etl
set the default roles of user {p}_etl to {p}_loader
"""
# Core's best-effort grants: a shortfall is a warning and never a `+` line (2.4).
CORE_BEST_EFFORT = (
    "SELECT ON system.parts",
    "SELECT ON system.replicas",
    "SYSTEM SYNC DATABASE REPLICA, SYSTEM SYNC REPLICA ON {p}_db.*",
    "CLUSTER ON *.*",
    "READ ON REMOTE",
    "CREATE TEMPORARY TABLE ON *.*",
)
IN_SYNC = "✓ In sync for it (access.yaml): nothing to change.\n"
# What `show_access` reads for the project's users and roles.
_ACCESS_TABLES = ("users", "roles", "grants", "role_grants")


def test_nothing_applied_lists_every_create_and_grant(access_project, clickhouse_server):
    project, p = access_project, access_project.name
    write_access(project, ACCESS_YAML)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == NOTHING_APPLIED.format(p=p).splitlines()
    assert [marked(result, marker) for marker in "-~!"] == [[], [], []]
    assert result.stdout.splitlines()[-1] == "→ 25 changes pending for it (access.yaml)."
    role = f"role {p}_migration_role"
    assert result.stderr.splitlines() == [
        f"! best-effort: {role} lacks {grant.format(p=p)}"
        for grant in core_best_effort(clickhouse_server)
    ]


def test_in_sync_after_the_admin_runs_the_validate_sql(access_project):
    write_access(access_project, ACCESS_YAML)
    apply_sql(access_project)
    result = access(access_project, "plan", "it")
    assert (result.exit_code, result.stdout, result.stderr) == (0, IN_SYNC, ""), result.output


def test_show_prints_a_declared_role_and_plan_accepts_the_output(access_project):
    """Saved as access.yaml, what `show` prints for the declared users and roles is in sync."""
    project, p = access_project, access_project.name
    write_access(project, ACCESS_YAML)
    passwords = apply_sql(project)
    before = show_access(project)
    shown = access(project, "show", "it")
    assert shown.exit_code == 0, shown.output
    assert show_access(project) == before, "show writes nothing"
    document = yaml.safe_load(shown.stdout)
    assert document["roles"][f"{p}_analyst"] == {"grants": ["SELECT, SHOW TABLES ON {db}.*"]}
    assert document["users"][f"{p}_etl"] == {
        "roles": [f"{p}_analyst", f"{p}_loader"],
        "default_roles": [f"{p}_loader"],
        "grants": ["SELECT ON {db}.regions"],
    }
    assert not any(password in shown.output for password in passwords)
    assert_plan_accepts(project, shown)


def test_show_prints_a_role_named_on_the_command_line_and_plan_accepts_it(access_project):
    """A role access.yaml doesn't declare: its output is where adopting it starts."""
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    passwords = apply_sql(project)
    run_all(
        project,
        f"CREATE ROLE {p}_adhoc",
        f"GRANT ALTER UPDATE, ALTER DELETE ON {db}.* TO {p}_adhoc",
        f"GRANT SELECT ON {db}.events TO {p}_adhoc WITH GRANT OPTION",
        f"GRANT READ ON REMOTE TO {p}_adhoc",
        f"GRANT READ, WRITE ON S3 TO {p}_adhoc",
        f"GRANT {p}_analyst TO {p}_adhoc",
    )
    before = show_access(project)
    shown = access(project, "show", "it", f"{p}_adhoc")
    assert (shown.exit_code, shown.stderr) == (0, ""), shown.output
    assert show_access(project) == before, "show writes nothing"
    grants = [
        "ALTER DELETE, ALTER UPDATE ON {db}.*",
        "READ ON REMOTE",
        "READ, WRITE ON S3",
        "SELECT ON {db}.events WITH GRANT OPTION",
    ]
    assert yaml.safe_load(shown.stdout) == {
        "roles": {f"{p}_adhoc": {"grants": grants, "roles": [f"{p}_analyst"]}}
    }
    assert not any(password in shown.output for password in passwords)
    assert_plan_accepts(project, shown)


def test_a_grant_access_yaml_cannot_declare_is_left_out_of_show_and_revoked_by_plan(
    access_project,
):
    """`SET DEFINER ON <user>` is on neither a database, a table nor a data source.

    `TABLE ENGINE ON MergeTree` is another, but ClickHouse Cloud's admin can't give it.
    """
    project, role = access_project, f"{access_project.name}_adhoc"
    definer = f"SET DEFINER ON {access_project.name}_definer"
    write_access(project, f"roles: {{{role}: {{}}}}\n")
    apply_sql(project)
    project.client.command(f"GRANT {definer} TO {role}")
    shown = access(project, "show", "it")
    assert (shown.exit_code, yaml.safe_load(shown.stdout)) == (0, {"roles": {role: {}}})
    assert shown.stderr == (
        f"! role {role} holds {definer}, which access.yaml can't declare, so it is left out.\n"
    )
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "-") == [f"revoke {definer} from role {role}"]
    project.client.command(f"REVOKE {definer} FROM {role}")
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output


def test_show_puts_the_migration_role_under_migration_role(access_project):
    """Its profile, and what it holds beyond it. Saved, `plan` has no `!` line left."""
    project = access_project
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    shown = access(project, "show", "it", project.migration_role)
    assert shown.exit_code == 0, shown.output
    assert yaml.safe_load(shown.stdout) == {
        "migration_role": {
            "profile": "core",
            "grants": ["CREATE FUNCTION, DROP FUNCTION ON *.*"],
        }
    }
    write_access(project, shown.stdout)
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output


def test_show_refuses_what_access_yaml_cannot_declare_or_the_server_lacks(access_project):
    project, p = access_project, access_project.name
    write_access(project, ACCESS_YAML)
    admin = project.admin
    expected = {
        admin: f"✗ {admin} is config.yaml's admin_user, which ch-migrate never changes, "
        "so access.yaml can't declare it.\n",
        project.migration_user: f"✗ {p}_migration is the migration user, which `bootstrap` "
        "manages, so access.yaml can't declare it.\n",
        f"{p}_nobody": f"✗ The server has no user or role named {p}_nobody.\n",
    }
    for name, message in expected.items():
        result = access(project, "show", "it", name)
        assert (result.exit_code, result.stdout, result.stderr) == (2, "", message)
    result = access(project, "show", "it")
    assert (result.exit_code, yaml.safe_load(result.stdout)) == (0, {}), result.output
    assert f"! {p}_analyst is declared but isn't on the server, so it isn't shown." in (
        result.stderr.splitlines()
    )


def test_drift_a_hand_grant_is_one_revoke_and_a_hand_revoke_is_one_grant(access_project):
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    project.client.command(f"GRANT INSERT ON {db}.* TO {p}_analyst")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "-") == [f"revoke INSERT ON {db}.* from role {p}_analyst"]
    assert marked(result, "+") == []
    assert marked(result, "~") == [
        f"adopt role {p}_analyst: it exists on the server, and access.yaml now manages it"
    ]
    assert result.stdout.splitlines()[-1] == "→ 1 change pending for it (access.yaml)."

    project.client.command(f"REVOKE INSERT ON {db}.* FROM {p}_analyst")
    project.client.command(f"REVOKE SHOW TABLES ON {db}.* FROM {p}_analyst")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == [f"grant SHOW TABLES ON {db}.* to role {p}_analyst"]
    assert marked(result, "-") == []


def test_drift_in_memberships_options_and_default_roles(access_project):
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    run_all(
        project,
        f"GRANT {p}_loader TO {p}_dashboards",
        f"REVOKE {p}_analyst FROM {p}_reporting",
        f"GRANT {p}_analyst TO {p}_etl WITH ADMIN OPTION",
        f"SET DEFAULT ROLE ALL TO {p}_etl",
        f"GRANT SELECT ON {db}.* TO {p}_analyst WITH GRANT OPTION",
    )
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == [
        f"grant role {p}_analyst to role {p}_reporting",
        f"set the default roles of user {p}_etl to {p}_loader",
    ]
    assert marked(result, "-") == [
        f"revoke the grant option for SELECT ON {db}.* from role {p}_analyst",
        f"revoke role {p}_loader from user {p}_dashboards",
        f"revoke the admin option for role {p}_analyst from user {p}_etl",
    ]


def test_a_partial_revoke_is_drift_that_granting_again_clears(access_project):
    """U2: a revoke inside a declared grant is a row of its own; the `+` line re-grants."""
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    project.client.command(f"REVOKE SELECT ON {db}.secrets FROM {p}_analyst")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == [f"grant SELECT ON {db}.* to role {p}_analyst"]
    assert marked(result, "-") == []
    project.client.command(f"GRANT SELECT ON {db}.* TO {p}_analyst")
    assert access(project, "plan", "it").exit_code == 0


def test_narrowing_a_privilege_revokes_only_the_other_leaves(access_project):
    """ALTER UPDATE declared, ALTER held: one revoke of the other ALTER leaves, never ALTER."""
    project, p, db = access_project, access_project.name, access_project.database
    role = f"{p}_editor"
    write_access(project, f"roles: {{{role}: {{grants: ['ALTER UPDATE ON {{db}}.*']}}}}\n")
    apply_sql(project)
    project.client.command(f"GRANT ALTER ON {db}.* TO {role}")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == []
    [revoke] = marked(result, "-")
    match = re.fullmatch(rf"revoke (.+) ON {db}\.\* from role {role}", revoke)
    assert match, revoke
    names = match[1].split(", ")
    tree = PrivilegeTree.read(project.client)
    revoked = frozenset().union(*(tree.flags(name, "database") for name in names))
    assert revoked == tree.flags("ALTER", "database") - {"ALTER UPDATE"}
    assert "ALTER" not in names and len(names) < len(revoked)
    project.client.command(f"REVOKE {match[1]} ON {db}.* FROM {role}")
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output


def test_narrowing_a_scope_is_one_replace_of_the_declared_set(access_project):
    """Decision 3: `db.*` held and `db.events` declared can't be a grant and a revoke."""
    project, p, db = access_project, access_project.name, access_project.database
    role = f"{p}_reader"
    write_access(project, f"roles: {{{role}: {{grants: ['SELECT ON {{db}}.events']}}}}\n")
    apply_sql(project)
    project.client.command(f"REVOKE SELECT ON {db}.events FROM {role}")
    project.client.command(f"GRANT SELECT ON {db}.* TO {role}")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == []
    assert marked(result, "-") == [
        f"replace the grants of role {role} with its declared set in one statement, "
        f"instead of: revoke SELECT ON {db}.* from role {role}"
    ]
    # What `apply` will run for it: one statement, and the role then matches.
    project.client.command(f"GRANT SELECT ON {db}.events TO {role} WITH REPLACE OPTION")
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output


def test_a_replace_names_every_grant_and_revoke_it_stands_in_for(access_project):
    """Decision 3: `plan` shows every revoke, so the one line names all a replace takes away."""
    project, p, db = access_project, access_project.name, access_project.database
    role = f"{p}_reader"
    grants = "['SELECT ON {db}.events', 'INSERT ON {db}.events WITH GRANT OPTION']"
    write_access(project, f"roles: {{{role}: {{grants: {grants}}}}}\n")
    run_all(
        project,
        f"CREATE ROLE {role}",
        f"GRANT SELECT, SHOW TABLES ON {db}.* TO {role}",
        f"REVOKE SELECT ON {db}.events FROM {role}",
        f"GRANT INSERT ON {db}.* TO {role} WITH GRANT OPTION",
        f"GRANT CREATE ROLE ON *.* TO {role}",
    )
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    [replace] = [line for line in marked(result, "-") if role in line]
    assert replace == (
        f"replace the grants of role {role} with its declared set in one statement, instead of: "
        f"grant SELECT ON {db}.events to role {role}; "
        f"revoke CREATE ROLE ON *.* from role {role}; "
        f"revoke INSERT, SELECT, SHOW TABLES ON {db}.* from role {role}"
    )
    assert [line for line in marked(result, "+") if role in line] == []


def test_migration_role_extras_are_notes_that_leave_the_exit_code_at_0(access_project):
    """Nothing is revoked from the migration role: what it holds beyond its profile is listed."""
    project, p, db = access_project, access_project.name, access_project.database
    role = project.migration_role
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    run_all(
        project,
        f"GRANT CREATE ROLE ON *.* TO {role}",
        f"GRANT SELECT ON {db}.* TO {role} WITH GRANT OPTION",
        f"GRANT INSERT ON other_db.* TO {role} WITH GRANT OPTION",
        f"GRANT {p}_analyst TO {role}",
    )
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert result.stdout.splitlines() == [
        f"! role {role} holds CREATE ROLE ON *.* beyond its profile",
        f"! role {role} holds INSERT ON other_db.* WITH GRANT OPTION beyond its profile",
        f"! role {role} holds the grant option for SELECT ON {db}.* beyond its profile",
        f"! role {role} holds role {p}_analyst beyond its profile",
        IN_SYNC.strip(),
    ]


def test_migration_role_missing_a_required_grant_is_a_change(access_project):
    """Core names ALTER DELETE in two of its entries, so each entry gets its `+` line."""
    project, db, role = access_project, access_project.database, access_project.migration_role
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    project.client.command(f"GRANT CREATE ROLE ON *.* TO {role}")
    project.client.command(f"REVOKE SELECT ON system.mutations FROM {role}")
    project.client.command(f"REVOKE ALTER DELETE, ALTER UPDATE ON {db}.* FROM {role}")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == [
        f"grant ALTER DELETE, ALTER UPDATE ON {db}.* to role {role}",
        f"grant ALTER DELETE ON {db}.* to role {role}",
        f"grant SELECT ON system.mutations to role {role}",
    ]
    assert marked(result, "-") == [] and marked(result, "~") == []
    assert marked(result, "!") == [f"role {role} holds CREATE ROLE ON *.* beyond its profile"]
    assert result.stdout.splitlines()[-1] == "→ 3 changes pending for it (access.yaml)."


def test_legacy_mode_compares_only_the_migration_role_and_user(
    bootstrapped_project, clickhouse_server
):
    """No access.yaml: 0.8's `bootstrap` leaves the legacy profile exactly, with no extras.

    Each best-effort grant the admin can't give in full is a warning: none on a stock server.
    """
    project, role = bootstrapped_project, bootstrapped_project.migration_role
    db = project.database
    assert_legacy_in_sync(project, clickhouse_server)

    # Adopting access.yaml with the core profile keeps 0.8's grants: they are extras.
    write_access(project, "{}\n")
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert marked(result, "!") == [
        f"role {role} holds ALTER ROLE, ALTER USER, CREATE FUNCTION, CREATE ROLE, CREATE USER, "
        "DROP FUNCTION, DROP ROLE, DROP USER, ROLE ADMIN ON *.* WITH GRANT OPTION "
        "beyond its profile",
        f"role {role} holds ACCESS MANAGEMENT ON {db}.* WITH GRANT OPTION beyond its profile",
        f"role {role} holds SELECT ON system.* WITH GRANT OPTION beyond its profile",
        f"role {role} holds the grant option for CREATE TEMPORARY TABLE ON *.* "
        "beyond its profile",
        f"role {role} holds the grant option for ALTER, CREATE DICTIONARY, CREATE TABLE, "
        "CREATE VIEW, DROP DICTIONARY, DROP TABLE, DROP VIEW, INSERT, OPTIMIZE, SELECT, "
        f"SHOW COLUMNS, SHOW DICTIONARIES, SHOW TABLES, TRUNCATE, UNDROP TABLE ON {db}.* "
        "beyond its profile",
    ]
    assert marked(result, "+") + marked(result, "-") + marked(result, "~") == []
    project.client.command(f"REVOKE {role} FROM {project.migration_user}")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == [f"grant role {role} to user {project.migration_user}"]


def test_best_effort_grant_the_admin_cannot_give_is_a_warning(
    access_project, limited_admin, clickhouse_server
):
    """The limited admin runs the SQL: `CURRENT GRANTS` lands nothing it doesn't hold itself."""
    project, p = access_project, access_project.name
    best_effort = "{grant: CLUSTER ON *.*, best_effort: true}"
    write_access(
        project,
        f"roles: {{'{{project}}_ops': {{grants: ['SELECT ON {{db}}.*', {best_effort}]}}}}\n",
    )
    admin = limited_admin.connect()
    try:
        apply_sql(project, admin)
    finally:
        admin.close()
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout) == (0, IN_SYNC), result.output
    cluster = ["CLUSTER ON *.*"] if requires_cluster_grant(clickhouse_server) else []
    unreachable = [*cluster, "READ ON REMOTE", "CREATE TEMPORARY TABLE ON *.*"]
    assert result.stderr.splitlines() == [
        *(f"! best-effort: role {p}_migration_role lacks {grant}" for grant in unreachable),
        *(f"! best-effort: role {p}_ops lacks {grant}" for grant in cluster),
    ]


def test_an_undeclared_user_is_never_mentioned(access_project):
    """Not even one that holds a declared role, while that role has changes pending."""
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML)
    apply_sql(project)
    stranger = f"{p}_stranger"
    run_all(
        project,
        f"CREATE USER {stranger} IDENTIFIED BY '{project.new_password()}'",
        f"GRANT {p}_analyst TO {stranger}",
        f"GRANT INSERT ON {db}.* TO {stranger}",
        f"GRANT INSERT ON {db}.* TO {p}_analyst",
    )
    result = access(project, "plan", "it")
    assert result.exit_code == 1 and len(marked(result, "-")) == 1, result.output
    shown = access(project, "show", "it")
    assert shown.exit_code == 0, shown.output
    assert stranger not in result.output + shown.output


def test_plan_writes_nothing(access_project):
    """What the server holds is the same before and after, with changes pending in every
    direction."""
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, ACCESS_YAML + f"  '{{project}}_gone': {{absent: true}}\n")
    apply_sql(project)
    run_all(
        project,
        f"CREATE USER {p}_gone IDENTIFIED BY '{project.new_password()}'",
        f"GRANT INSERT ON {db}.* TO {p}_analyst",
        f"REVOKE SELECT ON {db}.events FROM {p}_loader",
        f"DROP ROLE {p}_reporting",
        f"GRANT CREATE ROLE ON *.* TO {project.migration_role}",
    )
    before = show_access(project)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert f"drop user {p}_gone" in marked(result, "-")
    assert f"create role {p}_reporting" in marked(result, "+")
    assert show_access(project) == before


def test_no_secret_reaches_the_output(access_project, monkeypatch, clickhouse_server):
    """`plan` and `show` read only the admin's secret, and print none."""
    project = access_project
    sentinels = {name: f"Sentinel!{secrets.token_hex(12)}" for name in ("ETL", "DASHBOARDS")}
    for name, value in sentinels.items():
        monkeypatch.setenv(f"CH_IT_{name}_PASSWORD", value)
    monkeypatch.delenv("CH_IT_MIGRATION_PASSWORD")
    write_access(project, ACCESS_YAML)
    hidden = [*sentinels.values(), clickhouse_server.password]
    outputs = [access(project, "plan", "it").output]
    hidden += apply_sql(project)
    outputs += [access(project, command, "it").output for command in ("plan", "show")]
    project.configure(port=closed_port())
    outputs += [access(project, command, "it").output for command in ("plan", "show")]
    assert all(outputs)
    for output in outputs:
        assert not any(secret in output for secret in hidden), output


def test_the_admin_secret_is_needed_and_named(access_project, monkeypatch):
    write_access(access_project, ACCESS_YAML)
    monkeypatch.delenv("CH_IT_ADMIN_PASSWORD")
    for command in ("plan", "show"):
        result = access(access_project, command, "it")
        assert (result.exit_code, result.stdout) == (2, ""), result.output
        assert result.stderr.startswith("✗ CH_IT_ADMIN_PASSWORD is required.")


def test_unreachable_host_exits_2(access_project):
    write_access(access_project, ACCESS_YAML)
    access_project.configure(port=closed_port())
    for command in ("plan", "show"):
        result = access(access_project, command, "it")
        assert (result.exit_code, result.stdout) == (2, ""), result.output
        assert result.stderr.startswith("✗ ") and len(result.stderr.splitlines()) == 1


def test_an_invalid_access_yaml_exits_2_before_connecting(access_project):
    write_access(access_project, "roles: {a: {grant: []}}\n")
    access_project.configure(port=closed_port())
    result = access(access_project, "plan", "it")
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr.splitlines()[-1] == "✗ Invalid: 1 error."


@pytest.mark.parametrize(
    ("declaration", "error"),
    [
        (
            "roles: {'{project}_a': {roles: ['{project}_missing']}}",
            "role {p}_a is declared to hold role {p}_missing, "
            "which access.yaml doesn't declare and the server doesn't have.",
        ),
        (
            "users: {'{project}_u': {}}",
            "user {p}_u isn't on the server and has no password, so it can't be created. "
            "Set `password: {{secret: NAME}}`, or create the user first.",
        ),
        (
            "roles: {'{project}_a': {grants: ['SELEKT ON {db}.*']}}",
            "role {p}_a: `SELEKT ON {p}_db.*`: the server has no privilege named SELEKT.",
        ),
        (
            "roles: {'{project}_a': {grants: [READ ON NOWHERE]}}",
            "role {p}_a: `READ ON NOWHERE`: the server has no data source named NOWHERE.",
        ),
        (
            "roles: {'{project}_a': {grants: [READ ON Distributed]}}",
            "role {p}_a: `READ ON Distributed`: the server stores Distributed under another "
            "name: write the source's own name, in upper case, such as REMOTE.",
        ),
    ],
)
def test_what_the_server_cannot_do_exits_2_and_is_named(access_project, declaration, error):
    """`validate` only warns about a name it doesn't know; `plan` asks the server (2.5)."""
    write_access(access_project, declaration + "\n")
    assert access(access_project, "validate", "it").exit_code == 0
    result = access(access_project, "plan", "it")
    assert (result.exit_code, result.stdout) == (2, ""), result.output
    assert result.stderr == f"✗ {error.format(p=access_project.name)}\n"


def test_names_the_bundled_list_misses_are_checked_on_the_server(access_project):
    """YTSAURUS isn't in the bundled 26.3 list, so `validate` warns; the server takes it."""
    project, p = access_project, access_project.name
    if not project.client.command("CHECK GRANT READ ON YTSAURUS"):
        pytest.skip(
            "The admin holds no READ ON YTSAURUS to give: ClickHouse Cloud's admin holds no "
            "source the bundled 26.3 list lacks"
        )
    write_access(project, "roles: {'{project}_a': {grants: [READ ON YTSAURUS]}}\n")
    assert "no source named YTSAURUS" in access(project, "validate", "it").stderr
    apply_sql(project)
    project.client.command(f"REVOKE READ ON YTSAURUS FROM {p}_a")
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stderr) == (1, ""), result.output
    assert marked(result, "+") == [f"grant READ ON YTSAURUS to role {p}_a"]
    project.client.command(f"GRANT READ ON YTSAURUS TO {p}_a")
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout, result.stderr) == (0, IN_SYNC, ""), result.output


def test_a_user_with_no_password_that_exists_is_managed_as_declared(access_project):
    project, p, db = access_project, access_project.name, access_project.database
    write_access(project, f"users: {{'{{project}}_u': {{grants: ['SELECT ON {{db}}.*']}}}}\n")
    project.client.command(f"CREATE USER {p}_u IDENTIFIED BY '{project.new_password()}'")
    apply_sql(project)
    project.client.command(f"REVOKE SELECT ON {db}.* FROM {p}_u")
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert marked(result, "+") == [f"grant SELECT ON {db}.* to user {p}_u"]


def test_version_floor_show_and_plan_need_25_8(access_project):
    """On an older server both exit 2 and name its version, before reading anything else."""
    project = access_project
    version = str(project.client.server_version)
    old = tuple(int(part) for part in version.split(".")[:2]) < (25, 8)
    write_access(project, ACCESS_YAML)
    results = {command: access(project, command, "it") for command in ("show", "plan")}
    if not old:
        assert [result.exit_code for result in results.values()] == [0, 1]
        return
    for result in results.values():
        assert (result.exit_code, result.stdout) == (2, ""), result.output
        assert result.stderr == (
            f"✗ The server runs ClickHouse {version}. "
            "`bootstrap` and the `access` commands that connect need 25.8 or later.\n"
        )


@pytest.mark.cluster
def test_local_storage_is_detected_and_read_once(clickhouse_cluster, tmp_path, monkeypatch):
    """Each node's own disk can't be behind itself, so a missing role is absent at once."""
    project = cluster_project(clickhouse_cluster, tmp_path, monkeypatch)
    live = read_live(project.client, Wanted(()))
    assert (live.storage, live.may_lag) == (LOCAL_STORAGE, False)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert f"create role {project.name}_a" in marked(result, "+")
    assert "Reading again" not in result.stderr


@pytest.mark.cluster
def test_keeper_storage_is_detected_and_every_replica_is_read_at_once(
    clickhouse_cluster_keeper, tmp_path, monkeypatch
):
    """With users and roles in Keeper a replica may be behind, so every replica is read in
    one query. They agree that the role is missing, so it is absent with no wait (T8)."""
    project = cluster_project(clickhouse_cluster_keeper, tmp_path, monkeypatch)
    live = read_live(project.client, Wanted(()))
    assert (live.storage, live.may_lag) == (REPLICATED_STORAGE, True)
    result = access(project, "plan", "it")
    assert result.exit_code == 1, result.output
    assert f"create role {project.name}_a" in marked(result, "+")
    assert not any(line.startswith("→ ") for line in result.stderr.splitlines()), result.stderr


def assert_legacy_in_sync(project, server):
    """Legacy `plan` is in sync, and warns once for each legacy shortfall."""
    in_sync = (
        "✓ In sync for it (legacy mode, the migration role and user only): nothing to change.\n"
    )
    result = access(project, "plan", "it")
    assert (result.exit_code, result.stdout) == (0, in_sync), result.output
    warnings = result.stderr.splitlines()
    assert len(warnings) == len(legacy_shortfalls(project, server)), result.stderr
    lacks = f"! best-effort: role {project.migration_role} lacks "
    assert all(line.startswith(lacks) for line in warnings), result.stderr


def legacy_shortfalls(project, server):
    """The legacy profile's best-effort grants the admin can't give in full.

    None on a stock server. ClickHouse Cloud's admin holds `ALTER USER` and `DROP USER` with
    Cloud's own console user revoked, and `SELECT ON *.*` with `system.zookeeper` revoked.
    CLUSTER needs no grant there.
    """
    profile = yaml.safe_load(
        (Path(profiles.__file__).parent / "profiles" / "legacy.yaml").read_text()
    )
    grants = [entry["grant"] for entry in profile["grants"] if isinstance(entry, dict)]
    if not requires_cluster_grant(server):
        grants.remove("CLUSTER ON *.*")
    client = project.client
    return [g for g in grants if not gives_in_full(client, g.format(db=project.database))]


def core_best_effort(server):
    """Core's best-effort grants the migration role lacks with nothing applied: CLUSTER only
    where ON CLUSTER needs the grant, so not on ClickHouse Cloud, which gives it to all."""
    if requires_cluster_grant(server):
        return CORE_BEST_EFFORT
    return tuple(grant for grant in CORE_BEST_EFFORT if grant != "CLUSTER ON *.*")


def access(project, *args):
    """Run `ch-migrate access ...`. Only an exit code may end it, never an exception."""
    result = project.run("access", *args)
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result


def assert_plan_accepts(project, shown):
    """Saved as access.yaml, `show`'s output leaves `plan` nothing to change."""
    write_access(project, shown.stdout)
    result = access(project, "plan", "it")
    assert result.exit_code == 0, result.output
    assert marked(result, "+") + marked(result, "-") + marked(result, "~") == []


def marked(result, marker):
    """The plan lines that start with this marker, without it."""
    return [line[2:] for line in result.stdout.splitlines() if line.startswith(marker + " ")]


def write_access(project, text):
    (project.root / "access.yaml").write_text(text)


def apply_sql(project, client=None):
    """Run the SQL from `access validate --sql`, each `<secret:NAME>` a quoted value.

    Returns the passwords it set, for the tests that look for them in output.
    """
    result = access(project, "validate", "it", "--sql")
    assert result.exit_code == 0, result.output
    passwords = []
    for statement in split_statements(result.stdout):
        sql = statement.sql
        if "<secret:" in sql:
            passwords.append(project.new_password())
            sql = re.sub(r"<secret:\w+>", f"'{passwords[-1]}'", sql)
        (client or project.client).command(sql)
    return passwords


def run_all(project, *statements):
    for statement in statements:
        project.client.command(statement)


def show_access(project):
    """What the server holds, as the admin can read it: `SHOW ACCESS` where it may run it, and
    the project's users and roles, their grants and the roles they hold, everywhere.

    ClickHouse Cloud's admin lacks `SHOW ACCESS ON *.*`. There the project's own rows stand
    for it: every name the access commands could touch starts with the project's.
    """
    shown = {table: _project_rows(project, table) for table in _ACCESS_TABLES}
    try:
        shown["SHOW ACCESS"] = project.client.query("SHOW ACCESS").result_rows
    except DatabaseError as error:
        if "ACCESS_DENIED" not in str(error):
            raise
    return shown


def closed_port():
    """A local port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _project_rows(project, table):
    """The rows of `system.<table>` for the project's users and roles, in a stable order."""
    columns = ("name",) if table in ("users", "roles") else ("user_name", "role_name")
    where = " OR ".join(f"startsWith({column}, {{p:String}})" for column in columns)
    rows = project.client.query(
        f"SELECT * FROM system.{table} WHERE {where}", parameters={"p": project.name}
    ).result_rows
    return sorted(repr(row) for row in rows)


def cluster_project(cluster, tmp_path, monkeypatch):
    """A project on node 1 that declares one role and waits 0.3 s for a replica."""
    name = "it_" + secrets.token_hex(8)
    project = AccessProject(tmp_path, cluster.clients[1], name)
    initialize_cluster_project(cluster, project, monkeypatch)
    project.configure(access_read_timeout=0.3)
    write_access(project, "roles: {'{project}_a': {}}\n")
    return project
