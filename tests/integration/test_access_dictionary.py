"""`create_dictionary` in a project with an access.yaml: the reader is declared, not granted.

The migration role holds `core`, which can't grant, so the helper only creates the
dictionary. access.yaml declares the reader and its SELECT, and the reader's password comes
from the environment's `dict_reader_password` secret, as `{secret: dict_reader_password}`
reads it.
"""

import secrets

import pytest

pytestmark = pytest.mark.integration

ACCESS_YAML = """\
migration_role:
  profile: core
users:
  {reader}:
    password: {{secret: dict_reader_password}}
    grants:
      - SELECT ON {{db}}.events
"""

DICTIONARY_SQL = (
    "CREATE DICTIONARY IF NOT EXISTS {{db}}.statuses (id UInt64, status String) PRIMARY KEY id "
    "SOURCE(CLICKHOUSE(DB '{{db}}' TABLE 'events' USER '{reader}' PASSWORD '{{password}}')) "
    "LAYOUT(FLAT()) LIFETIME(0)"
)

REVISION = """\
from ch_migrate import create_dictionary

revision = "dict_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    create_dictionary("dict.sql")


def downgrade():
    pass
"""


def test_a_declared_reader_reads_the_dictionary_source(access_project, monkeypatch):
    project = access_project
    reader = project.name + "_dict_reader"
    monkeypatch.delenv("DICT_READER_PASSWORD", raising=False)
    # SQLAlchemy read a leading `:` as a bind parameter, and doubles `%`.
    # The reader logs in to read the source, so the password arrived as it was set.
    monkeypatch.setenv("CH_IT_DICT_READER_PASSWORD", ":" + project.new_password() + "%")
    (project.root / "access.yaml").write_text(ACCESS_YAML.format(reader=reader))
    _run(project, "bootstrap", "it")
    _run(project, "access", "apply", "it")
    project.client.command(
        f"CREATE TABLE {project.database}.events (id UInt64, status String) "
        "ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {project.database}.events VALUES (1, 'new')")
    sql_dir = project.root / "migrations" / "sql"
    (sql_dir / "dict.sql").write_text(DICTIONARY_SQL.format(reader=reader))
    (project.root / "migrations" / "versions" / "dict_001.py").write_text(REVISION)

    _run(project, "up", "it")

    looked_up = project.client.query(
        f"SELECT dictGet('{project.database}.statuses', 'status', toUInt64(1))"
    ).result_rows
    assert looked_up == [("new",)]
    # The migration granted nothing that `apply` would revoke.
    _run(project, "access", "plan", "it")


def _run(project, *args):
    result = project.run(*args)
    assert result.exit_code == 0, (args, result.output)
    return result


def test_a_failed_dictionary_never_prints_its_password(access_project, monkeypatch):
    """The statement carries the reader's password as a literal, and
    `up --verbose` printed SQLAlchemy's message, which quotes the statement. That
    message doubles `%`, and a leading `:` was named as a missing bind parameter."""
    project = access_project
    reader = project.name + "_dict_reader"
    core = secrets.token_hex(24)  # every encoding and escape of the password keeps this
    password = f":{core}%/+&Chm!"
    monkeypatch.delenv("DICT_READER_PASSWORD", raising=False)
    monkeypatch.setenv("CH_IT_DICT_READER_PASSWORD", password)
    (project.root / "access.yaml").write_text(ACCESS_YAML.format(reader=reader))
    _run(project, "bootstrap", "it")
    broken = DICTIONARY_SQL.replace("LAYOUT(FLAT())", "LAYOUT(NO_SUCH_LAYOUT())")
    (project.root / "migrations" / "sql" / "dict.sql").write_text(broken.format(reader=reader))
    (project.root / "migrations" / "versions" / "dict_001.py").write_text(REVISION)

    result = project.run("up", "it", "--verbose")

    assert result.exit_code != 0, result.output
    assert "Traceback" in result.output, result.output
    assert core not in result.output
