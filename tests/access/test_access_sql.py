"""The SQL `access validate --sql` prints: apply's order, best-effort grants, secret placeholders."""

import re

import pytest

from ch_migrate.access.render import render_sql, users_that_must_exist

FILE = """\
    migration_role:
      grants: [CREATE FUNCTION ON *.*]
    roles:
      reader:
        grants:
          - SELECT ON {db}.* WITH GRANT OPTION
          - {grant: CLUSTER ON *.*, best_effort: true}
          - {grant: SELECT ON system.* WITH GRANT OPTION, best_effort: true}
      old: {absent: true}
    users:
      etl:
        password: {secret: etl_password}
        roles: [reader, writer]
        default_roles: [reader]
        grants: ['DELETE ON {db}.events']
      viewer:
        roles: [reader]
        default_roles: []
        grants: ['SHOW TABLES ON {db}.*']
      gone: {absent: true}
    """
# Every user with no password, beside the migration user, which always has one.
NO_PASSWORDS = """\
    users:
      viewer: {roles: [reader], grants: ['SELECT ON {db}.*']}
      ops: {}
    """
PLACEHOLDER = re.compile(r"<secret:\w+>")
# The smallest access.yaml: an empty one is an error.
CORE_ONLY = "migration_role: {profile: core}\n"


def test_statements_follow_applys_order(validate):
    sql = render_sql(validate(FILE).spec)
    core = len(validate(CORE_ONLY).spec.migration.profile.grants)
    assert sql[:8] == [
        "CREATE ROLE IF NOT EXISTS shop_migration_role",
        "CREATE ROLE IF NOT EXISTS reader",
        "CREATE USER IF NOT EXISTS shop_migrator IDENTIFIED BY <secret:migration_password>",
        "CREATE USER IF NOT EXISTS etl IDENTIFIED BY <secret:etl_password>",
        "GRANT shop_migration_role TO shop_migrator",
        "GRANT reader, writer TO etl",
        "GRANT reader TO viewer",
        "GRANT CREATE TABLE, DROP TABLE, UNDROP TABLE, ALTER ON shop_dev.* TO shop_migration_role",
    ]
    assert sql[7 + core :] == [
        "GRANT CREATE FUNCTION ON *.* TO shop_migration_role",
        "GRANT SELECT ON shop_dev.* TO reader WITH GRANT OPTION",
        "GRANT CURRENT GRANTS(CLUSTER ON *.*) TO reader",
        "GRANT CURRENT GRANTS(SELECT ON system.*) TO reader WITH GRANT OPTION",
        "GRANT ALTER DELETE ON shop_dev.events TO etl",
        "GRANT SHOW TABLES ON shop_dev.* TO viewer",
        "SET DEFAULT ROLE reader TO etl",
        "SET DEFAULT ROLE NONE TO viewer",
        "DROP USER IF EXISTS gone",
        "DROP ROLE IF EXISTS old",
    ]


@pytest.mark.parametrize(
    "text", [FILE, NO_PASSWORDS, CORE_ONLY], ids=["mixed", "no-passwords", "core-only"]
)
def test_every_create_user_is_identified_by_an_unquoted_placeholder(validate, text):
    """A CREATE USER with no IDENTIFIED clause is a no_password login on 26.3, so none is printed.

    The placeholder is unquoted, so SQL run unedited fails to parse instead of setting the
    placeholder text as the password.
    """
    sql = render_sql(validate(text).spec)
    creates = [statement for statement in sql if statement.startswith("CREATE USER")]
    assert creates
    for statement in creates:
        assert re.fullmatch(r"CREATE USER IF NOT EXISTS \w+ IDENTIFIED BY <secret:\w+>", statement)
    assert all(
        PLACEHOLDER.search(statement) is None for statement in sql if statement not in creates
    )
    assert "'<secret:" not in "\n".join(sql)


def test_a_user_with_no_password_is_not_created_but_keeps_its_roles_and_grants(validate):
    spec = validate(NO_PASSWORDS).spec
    sql = render_sql(spec)
    assert users_that_must_exist(spec) == ["viewer", "ops"]
    assert [statement for statement in sql if statement.startswith("CREATE USER")] == [
        "CREATE USER IF NOT EXISTS shop_migrator IDENTIFIED BY <secret:migration_password>"
    ]
    assert "GRANT reader TO viewer" in sql
    assert "GRANT SELECT ON shop_dev.* TO viewer" in sql


def test_absent_users_are_not_users_that_must_exist(validate):
    assert users_that_must_exist(validate(FILE).spec) == ["viewer"]


def test_best_effort_profile_grants_use_current_grants(validate):
    sql = render_sql(validate(CORE_ONLY).spec)
    assert "GRANT CURRENT GRANTS(READ ON REMOTE) TO shop_migration_role" in sql
    assert "GRANT SELECT ON system.dictionaries TO shop_migration_role" in sql


def test_no_secret_is_read_even_when_one_is_set(validate, monkeypatch):
    monkeypatch.setenv("CH_DEV_ETL_PASSWORD", "sentinel-etl")
    monkeypatch.setenv("CH_DEV_MIGRATION_PASSWORD", "sentinel-migration")
    sql = "\n".join(render_sql(validate(FILE).spec))
    assert "sentinel" not in sql
    assert "IDENTIFIED BY <secret:etl_password>" in sql
