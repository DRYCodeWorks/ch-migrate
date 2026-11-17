"""The built-in profiles (2.4) and legacy mode, which must match 0.8's `bootstrap`."""

import re

import pytest
from click.testing import CliRunner

from ch_migrate.access.model import Grant, Target
from ch_migrate.access.privileges import bundled_tree
from ch_migrate.access.profiles import PROFILE_DIR, load_profiles
from ch_migrate.bootstrap import build_bootstrap_sql
from ch_migrate.cli import main
from ch_migrate.sql import split_statements

VALUES = {
    "db": "shop_dev",
    "project": "shop",
    "env": "dev",
    "migration_role": "shop_migration_role",
}
# The statements `validate --sql` must share with 0.8's `build_bootstrap_sql`.
COMPARED = re.compile(r"^(GRANT|CREATE ROLE|CREATE USER)\b")
# 0.8 quotes the password; `--sql` prints an unquoted placeholder that names the secret.
PASSWORD_0_8 = re.compile(r"IDENTIFIED BY '(?:[^'\\]|\\.|'')*'")
PLACEHOLDER = re.compile(r"IDENTIFIED BY <secret:\w+>")


@pytest.fixture(scope="module")
def profiles():
    profiles, reader = load_profiles(VALUES, bundled_tree())
    assert (reader.errors, reader.warnings) == ([], [])
    return profiles


def test_the_profiles_are_core_and_legacy_and_ship_as_yaml():
    assert sorted(path.name for path in PROFILE_DIR.glob("*.yaml")) == ["core.yaml", "legacy.yaml"]


def test_core_requires_the_database_rights_without_grant_option(profiles):
    required = [grant for grant in profiles["core"].grants if not grant.best_effort]
    on_db = {
        name
        for grant in required
        if grant.target == Target("shop_dev")
        for name in grant.privileges
    }
    assert on_db == {
        "CREATE TABLE", "DROP TABLE", "UNDROP TABLE", "ALTER",
        "CREATE VIEW", "DROP VIEW", "CREATE DICTIONARY", "DROP DICTIONARY",
        "SELECT", "INSERT", "DELETE", "TRUNCATE", "OPTIMIZE",
        "SHOW TABLES", "SHOW COLUMNS", "SHOW DICTIONARIES",
    }  # fmt: skip
    system = {grant.target.table for grant in required if grant.target.database == "system"}
    assert system == {"databases", "tables", "mutations", "dictionaries"}
    assert not any(grant.grant_option for grant in profiles["core"].grants)


def test_core_makes_cluster_and_system_extras_best_effort(profiles):
    best_effort = [str(grant) for grant in profiles["core"].grants if grant.best_effort]
    assert best_effort == [
        "SELECT ON system.parts",
        "SELECT ON system.replicas",
        "SYSTEM SYNC REPLICA, SYSTEM SYNC DATABASE REPLICA ON shop_dev.*",
        "CLUSTER ON *.*",
        "READ ON REMOTE",
        "CREATE TEMPORARY TABLE ON *.*",
    ]


def test_profiles_keep_0_8_spelling(profiles):
    """Built-in grants render as written, so legacy SQL is 0.8's SQL; DELETE stays DELETE."""
    assert (
        Grant(
            ("SELECT", "INSERT", "DELETE", "TRUNCATE", "OPTIMIZE"),
            Target("shop_dev"),
            grant_option=True,
        )
        in profiles["legacy"].grants
    )


@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize("dict_reader", [False, True])
def test_legacy_sql_matches_0_8_build_bootstrap_sql(project, mcp, dict_reader):
    """The same GRANT, CREATE ROLE and CREATE USER statements, ignoring order and passwords."""
    names = {
        "mcp_user_name": "shop_mcp" if mcp else None,
        "dict_reader_name": "shop_dict" if dict_reader else None,
    }
    _set_config(project, names)
    result = CliRunner().invoke(main, ["access", "validate", "dev", "--sql"])
    assert result.exit_code == 0, result.output
    expected = build_bootstrap_sql(
        project="shop",
        db="shop_dev",
        migration_user="shop_migrator",
        migration_password="pw",
        mcp_user_name=names["mcp_user_name"],
        mcp_password="pw" if mcp else None,
        dict_reader_name=names["dict_reader_name"],
        dict_reader_password="pw" if dict_reader else None,
    )
    assert _compared(result.stdout, PLACEHOLDER) == _compared(expected, PASSWORD_0_8)
    assert len(_compared(expected, PASSWORD_0_8)) == 26 + 5 * mcp + 3 * dict_reader


def _set_config(project, names):
    config = (project / "config.yaml").read_text()
    extra = "".join(f"    {key}: {value}\n" for key, value in names.items() if value)
    (project / "config.yaml").write_text(config.replace("    admin_user: shop_admin\n", extra, 1))


def _compared(sql, password):
    statements = (" ".join(statement.sql.split()) for statement in split_statements(sql))
    return sorted(password.sub("IDENTIFIED BY ?", s) for s in statements if COMPARED.match(s))
