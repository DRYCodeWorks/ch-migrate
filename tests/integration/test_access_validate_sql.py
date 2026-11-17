"""`access validate --sql` fails closed: an unedited password placeholder doesn't parse.

Each password is printed unquoted, `IDENTIFIED BY <secret:NAME>`. Run as printed, the server
rejects the statement with SYNTAX_ERROR and creates no user, instead of setting the
placeholder text as the password. Filled in with the quoted secret, as the header says, it runs.
"""

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from ch_migrate.sql import split_statements

pytestmark = pytest.mark.integration

ACCESS_YAML = """\
users:
  '{project}_etl': {password: {secret: etl_password}}
"""


def test_unedited_placeholders_fail_to_parse_and_create_no_user(access_project):
    (access_project.root / "access.yaml").write_text(ACCESS_YAML)
    result = access_project.run("access", "validate", "it", "--sql")
    assert result.exit_code == 0, result.output
    sql = [statement.sql for statement in split_statements(result.stdout)]
    creates = [statement for statement in sql if statement.startswith("CREATE USER")]
    assert len(creates) == 2, result.stdout
    for statement in creates:
        with pytest.raises(DatabaseError, match="SYNTAX_ERROR"):
            access_project.client.command(statement)
    assert _users(access_project) == []

    (etl,) = [statement for statement in creates if "<secret:etl_password>" in statement]
    password = access_project.new_password()
    access_project.client.command(etl.replace("<secret:etl_password>", f"'{password}'"))
    assert _users(access_project) == [access_project.name + "_etl"]


def _users(project):
    result = project.client.query(
        "SELECT name FROM system.users WHERE startsWith(name, {prefix:String}) ORDER BY name",
        parameters={"prefix": project.name},
    )
    return [name for (name,) in result.result_rows]
