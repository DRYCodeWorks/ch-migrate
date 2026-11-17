"""Pin what 0.8's `bootstrap` grants to a project that has no access.yaml.

`tests/fixtures/access/legacy_bootstrap_<major>_<minor>.json` records what `read_access` read
after each case below, on that ClickHouse version. The design note's `legacy` profile is
defined by it. It comes from a server and is never edited by hand. To record it again, run
this file with the variable set, then review the diff:

    CH_MIGRATE_RECORD_ACCESS=1 env -u CH_MIGRATE_IT_URL uv run --locked pytest -q \\
        -m integration tests/integration/test_access_characterization.py

`CH_MIGRATE_IT_IMAGE` picks the image; the file name follows the server's version. A version
with no recorded file skips.
"""

import json
import os
from pathlib import Path

import pytest
from access_fixtures import read_access

pytestmark = pytest.mark.integration

RECORD_VARIABLE = "CH_MIGRATE_RECORD_ACCESS"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "access"


def test_plain_bootstrap(bootstrapped_project):
    _assert_recorded(bootstrapped_project, "plain")


def test_bootstrap_with_mcp_user_and_dict_reader(access_project, monkeypatch):
    access_project.configure(
        mcp_user_name=access_project.name + "_mcp",
        dict_reader_name=access_project.name + "_dict",
    )
    monkeypatch.setenv("CH_IT_MCP_PASSWORD", access_project.new_password())
    monkeypatch.setenv("CH_IT_DICT_READER_PASSWORD", access_project.new_password())
    result = access_project.run("bootstrap", "it")
    assert result.exit_code == 0, result.output
    _assert_recorded(access_project, "mcp_user_and_dict_reader")


def test_limited_admin_names_each_grant_it_cannot_give(access_project, limited_admin):
    """T6 changed this on purpose (design note, Decision 6).

    0.8 stopped at `CREATE FUNCTION`, before it created the migration user. Now the role and
    the user come first, every grant the admin can give lands, and the exit is 1, naming each
    grant that failed. The recorded case was recorded again then.
    """
    result = access_project.run("bootstrap", "it")
    assert result.exit_code == 1, result.output
    role = access_project.migration_role
    failed = [line for line in result.stderr.splitlines() if line.startswith("✗ grant ")]
    assert [line.split(": ", 1)[0] for line in failed] == [
        f"✗ grant {grant} ON *.* WITH GRANT OPTION to role {role}"
        for grant in ("CREATE FUNCTION, DROP FUNCTION", "CREATE ROLE, ALTER ROLE, DROP ROLE")
    ] + [f"✗ grant ROLE ADMIN ON *.* WITH GRANT OPTION to role {role}"]
    assert all(line.endswith("(ACCESS_DENIED)") for line in failed), failed
    client = access_project.client
    assert client.command(f"EXISTS DATABASE {access_project.database}") == 1
    assert client.command(
        "SELECT count() FROM system.users WHERE name = {name:String}",
        parameters={"name": access_project.migration_user},
    )
    _assert_recorded(access_project, "limited_admin")


def _assert_recorded(project, case):
    """Compare with the recorded case; with `RECORD_VARIABLE=1`, record it first."""
    actual = read_access(project.client, *_legacy_names(project))
    path = FIXTURES / f"legacy_bootstrap_{_version(project.client)}.json"
    recorded = json.loads(path.read_text()) if path.exists() else {}
    if os.environ.get(RECORD_VARIABLE) == "1":
        recorded[case] = actual
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(recorded))
    if case not in recorded:
        pytest.skip(f"{path.name} has no {case!r} case; record it with {RECORD_VARIABLE}=1")
    assert actual == recorded[case]


def _legacy_names(project):
    """Every user and role 0.8's `bootstrap` can create, so the record shows which exist."""
    users = [project.migration_user, project.name + "_mcp", project.name + "_dict"]
    roles = [project.migration_role, project.name + "_readonly_role", project.name + "_dict_role"]
    return users, roles


def _version(client):
    major, minor = client.command("SELECT version()").split(".")[:2]
    return f"{major}_{minor}"


def _dump(recorded):
    """JSON with one row per line, so a changed grant shows as a one-line diff."""
    cases = []
    for case, tables in sorted(recorded.items()):
        blocks = [f"    {json.dumps(key)}: {_dump_rows(rows)}" for key, rows in tables.items()]
        cases.append(f"  {json.dumps(case)}: {{\n" + ",\n".join(blocks) + "\n  }")
    return "{\n" + ",\n".join(cases) + "\n}\n"


def _dump_rows(rows):
    if not rows:
        return "[]"
    lines = ",\n".join("      " + json.dumps(row, sort_keys=True) for row in rows)
    return "[\n" + lines + "\n    ]"
