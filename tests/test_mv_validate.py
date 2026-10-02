"""Tests for materialized view declaration validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ch_migrate.mv_validate import (
    MVDeclaration,
    MVValidationError,
    _find_grant_inserts,
    _find_grant_selects,
    _find_permissive_row_policies,
    _find_row_policies,
    _load_module_attribute,
    _sql_has_create_mv,
    validate_mv_migrations,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_migration(
    versions_dir: Path,
    name: str,
    *,
    sql: str = "",
    mv_declarations: str | None = None,
    skip_mv_validation: str | None = None,
    extra_python: str = "",
    create_date: str = "2026-03-26 10:00:00.000000",
    revision: str | None = None,
    down_revision: str | None = None,
) -> Path:
    """Write a migration file for testing.

    Args:
        versions_dir: Directory to write to
        name: Migration name (used for filename and revision)
        sql: SQL to put inside op.execute()
        mv_declarations: Python literal for MV_DECLARATIONS (as source code)
        skip_mv_validation: Python literal for skip_mv_validation (as source code)
        extra_python: Additional Python code after the imports
        create_date: Create Date header value
        revision: Override revision ID
        down_revision: Override down_revision
    """
    versions_dir.mkdir(parents=True, exist_ok=True)

    rev_id = revision or name[:12].ljust(12, "0")
    down_rev = f"'{down_revision}'" if down_revision else "None"

    parts = [
        f'"""{name}',
        "",
        f"Revision ID: {rev_id}",
        f"Revises: {down_revision or ''}",
        f"Create Date: {create_date}",
        "",
        '"""',
        "",
        "from alembic import op",
        "from ch_migrate import get_db",
        "",
        f"revision = '{rev_id}'",
        f"down_revision = {down_rev}",
        "branch_labels = None",
        "depends_on = None",
    ]

    if mv_declarations is not None:
        parts.append("")
        parts.append(f"MV_DECLARATIONS = {mv_declarations}")

    if skip_mv_validation is not None:
        parts.append("")
        parts.append(f"skip_mv_validation = {skip_mv_validation}")

    if extra_python:
        parts.append("")
        parts.append(extra_python)

    parts.extend([
        "",
        "def upgrade() -> None:",
        "    db = get_db()",
    ])

    if sql:
        parts.append(f'    op.execute(f"""{sql}""")')
    else:
        parts.append("    pass")

    parts.extend([
        "",
        "def downgrade() -> None:",
        "    pass",
        "",
    ])

    content = "\n".join(parts)
    file_path = versions_dir / f"{rev_id}_{name}.py"
    file_path.write_text(content)
    return file_path


# ---------------------------------------------------------------------------
# SQL pattern detection tests
# ---------------------------------------------------------------------------


class TestCreateMVDetection:
    def test_detects_create_mv(self):
        assert _sql_has_create_mv(
            "CREATE MATERIALIZED VIEW mydb.mv TO mydb.dest AS SELECT 1"
        )

    def test_detects_create_mv_if_not_exists(self):
        assert _sql_has_create_mv(
            "CREATE MATERIALIZED VIEW IF NOT EXISTS mydb.mv TO mydb.dest AS SELECT 1"
        )

    def test_case_insensitive(self):
        assert _sql_has_create_mv(
            "create materialized view mydb.mv TO mydb.dest AS SELECT 1"
        )

    def test_ignores_regular_view(self):
        assert not _sql_has_create_mv("CREATE VIEW mydb.v AS SELECT 1")

    def test_ignores_table(self):
        assert not _sql_has_create_mv(
            "CREATE TABLE mydb.t (id UInt64) ENGINE = MergeTree ORDER BY id"
        )

    def test_ignores_empty(self):
        assert not _sql_has_create_mv("")

    def test_ignores_drop_mv(self):
        assert not _sql_has_create_mv(
            "DROP MATERIALIZED VIEW IF EXISTS mydb.mv"
        )


class TestGrantInsertDetection:
    def test_finds_basic_grant(self):
        grants = _find_grant_inserts("GRANT INSERT ON db.my_table TO my_user")
        assert ("my_table", "my_user") in grants

    def test_finds_db_placeholder(self):
        grants = _find_grant_inserts(
            "GRANT INSERT ON {db}.my_table TO my_user"
        )
        assert ("my_table", "my_user") in grants

    def test_finds_multiple(self):
        sql = textwrap.dedent("""\
            GRANT INSERT ON {db}.table_a TO user_a;
            GRANT INSERT ON {db}.table_b TO user_b;
        """)
        grants = _find_grant_inserts(sql)
        assert ("table_a", "user_a") in grants
        assert ("table_b", "user_b") in grants

    def test_case_insensitive(self):
        grants = _find_grant_inserts("grant insert on {db}.MyTable to MyUser")
        assert ("mytable", "myuser") in grants

    def test_comma_separated_users(self):
        grants = _find_grant_inserts(
            "GRANT INSERT ON {db}.my_table TO user_a, user_b;"
        )
        assert ("my_table", "user_a") in grants
        assert ("my_table", "user_b") in grants

    def test_ignores_grant_select(self):
        grants = _find_grant_inserts("GRANT SELECT ON {db}.t TO u")
        assert len(grants) == 0


class TestGrantSelectDetection:
    def test_finds_basic_grant(self):
        grants = _find_grant_selects("GRANT SELECT ON {db}.my_table TO reader")
        assert ("my_table", "reader") in grants

    def test_finds_column_grant(self):
        grants = _find_grant_selects(
            "GRANT SELECT(col1, col2) ON {db}.my_table TO reader"
        )
        assert ("my_table", "reader") in grants


class TestRowPolicyDetection:
    def test_finds_basic_policy(self):
        policies = _find_row_policies(
            "CREATE ROW POLICY my_deny ON {db}.my_table USING 0 TO ALL"
        )
        assert ("my_deny", "my_table") in policies

    def test_finds_if_not_exists(self):
        policies = _find_row_policies(
            "CREATE ROW POLICY IF NOT EXISTS my_deny ON {db}.t USING 0 TO ALL"
        )
        assert ("my_deny", "t") in policies

    def test_finds_or_replace(self):
        policies = _find_row_policies(
            "CREATE ROW POLICY OR REPLACE my_access ON {db}.t USING 1 TO admin"
        )
        assert ("my_access", "t") in policies


class TestPermissiveRowPolicyDetection:
    def test_finds_permissive_policy(self):
        policies = _find_permissive_row_policies(
            "CREATE ROW POLICY my_policy ON {db}.logs USING 1 TO intake_writer;"
        )
        assert len(policies) == 1
        assert policies[0][0] == "logs"
        assert "intake_writer" in policies[0][1]

    def test_finds_multi_user(self):
        policies = _find_permissive_row_policies(
            "CREATE ROW POLICY my_policy ON {db}.logs USING 1 TO user_a, user_b;"
        )
        assert len(policies) == 1
        assert "user_a" in policies[0][1]
        assert "user_b" in policies[0][1]


# ---------------------------------------------------------------------------
# AST attribute loading tests
# ---------------------------------------------------------------------------


class TestLoadModuleAttribute:
    def test_loads_list(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text('MV_DECLARATIONS = [{"mv_name": "test"}]')
        result = _load_module_attribute(f, "MV_DECLARATIONS")
        assert result == [{"mv_name": "test"}]

    def test_loads_bool(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("skip_mv_validation = True")
        result = _load_module_attribute(f, "skip_mv_validation")
        assert result is True

    def test_returns_none_if_missing(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("revision = 'abc123'")
        result = _load_module_attribute(f, "MV_DECLARATIONS")
        assert result is None

    def test_returns_none_on_syntax_error(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("this is not valid python !!!!")
        result = _load_module_attribute(f, "MV_DECLARATIONS")
        assert result is None

    def test_returns_none_on_non_literal(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("MV_DECLARATIONS = some_function()")
        result = _load_module_attribute(f, "MV_DECLARATIONS")
        assert result is None


# ---------------------------------------------------------------------------
# Core validation tests
# ---------------------------------------------------------------------------


class TestMVDeclarationRequired:
    """Test that MV_DECLARATIONS is required when CREATE MATERIALIZED VIEW is present."""

    def test_missing_declarations_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src",
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "missing MV_DECLARATIONS" in errors[0].message

    def test_present_declarations_with_grants_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO intake_writer"
            ),
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["intake_writer"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_empty_declarations_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations="[]",
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "empty" in errors[0].message.lower()

    def test_non_list_declarations_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='{"mv_name": "test"}',
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "list" in errors[0].message.lower()

    def test_non_dict_entry_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='["not_a_dict"]',
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "dict" in errors[0].message.lower()

    def test_no_mv_no_error(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_table",
            sql="CREATE TABLE {db}.t (id UInt64) ENGINE = MergeTree ORDER BY id",
        )
        errors = validate_mv_migrations(versions)
        assert errors == []


class TestRequiredFields:
    """Test that required fields are validated in MV_DECLARATIONS."""

    def test_missing_mv_name_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"source_table": "src", "target_table": "dest", "inserting_users": ["u"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert any("mv_name" in e.message for e in errors)

    def test_missing_source_table_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"mv_name": "mv", "target_table": "dest", "inserting_users": ["u"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert any("source_table" in e.message for e in errors)

    def test_missing_target_table_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"mv_name": "mv", "source_table": "src", "inserting_users": ["u"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert any("target_table" in e.message for e in errors)

    def test_missing_inserting_users_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"mv_name": "mv", "source_table": "src", "target_table": "dest"}]',
        )
        errors = validate_mv_migrations(versions)
        assert any("inserting_users" in e.message for e in errors)

    def test_typo_in_required_field_errors(self, tmp_path: Path):
        """A typo like 'inseritng_users' should be caught as a missing required field."""
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"mv_name": "mv", "source_table": "src", "target_table": "dest", "inseritng_users": ["writer"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert any("inserting_users" in e.message for e in errors)


class TestGrantInsertValidation:
    """Test GRANT INSERT validation."""

    def test_missing_grant_insert_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["intake_writer"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "GRANT INSERT" in errors[0].message
        assert "dest" in errors[0].message
        assert "intake_writer" in errors[0].message

    def test_grant_in_same_migration_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO intake_writer"
            ),
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["intake_writer"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_grant_in_companion_migration_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["intake_writer"]}]',
            revision="aaa000000001",
        )
        _write_migration(
            versions,
            "grant_mv",
            sql="GRANT INSERT ON {db}.dest TO intake_writer",
            revision="bbb000000002",
            down_revision="aaa000000001",
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_multiple_inserting_users(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO user_a;\n"
                "GRANT INSERT ON {db}.dest TO user_b"
            ),
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["user_a", "user_b"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_missing_one_of_multiple_users_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO user_a"
            ),
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["user_a", "user_b"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "user_b" in errors[0].message

    def test_comma_separated_grant_to_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO user_a, user_b;"
            ),
            mv_declarations='[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest", "inserting_users": ["user_a", "user_b"]}]',
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_multiple_mvs_in_one_migration(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mvs",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv_a TO {db}.dest_a AS SELECT 1 FROM {db}.src;\n"
                "CREATE MATERIALIZED VIEW {db}.mv_b TO {db}.dest_b AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest_a TO writer;\n"
                "GRANT INSERT ON {db}.dest_b TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "mv_a", "source_table": "src", "target_table": "dest_a", "inserting_users": ["writer"]},'
                ' {"mv_name": "mv_b", "source_table": "src", "target_table": "dest_b", "inserting_users": ["writer"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert errors == []


class TestOptionalSelectUsers:
    """Test optional select_users validation."""

    def test_missing_grant_select_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "select_users": ["reader"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "GRANT SELECT" in errors[0].message
        assert "reader" in errors[0].message

    def test_grant_select_present_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer;\n"
                "GRANT SELECT ON {db}.dest TO reader"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "select_users": ["reader"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_no_select_users_skips_check(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert errors == []


class TestOptionalSourceRLS:
    """Test optional source_rls_users validation."""

    def test_missing_source_rls_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "source_rls_users": ["writer"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "ROW POLICY" in errors[0].message
        assert "src" in errors[0].message

    def test_source_rls_present_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer;\n"
                "CREATE ROW POLICY mv_permissive ON {db}.src USING 1 TO writer;"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "source_rls_users": ["writer"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_source_rls_no_substring_false_positive(self, tmp_path: Path):
        """User 'a' should NOT match against policy for 'admin'."""
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO a;\n"
                "CREATE ROW POLICY mv_permissive ON {db}.src USING 1 TO admin;"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["a"], "source_rls_users": ["a"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert any("ROW POLICY" in e.message and "a" in e.message for e in errors)


class TestOptionalTargetRLS:
    """Test optional target_rls_policies validation."""

    def test_missing_target_rls_errors(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "target_rls_policies": ["default_deny"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "default_deny" in errors[0].message

    def test_target_rls_present_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer;\n"
                "CREATE ROW POLICY dest_default_deny ON {db}.dest USING 0 TO ALL;"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "target_rls_policies": ["default_deny"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert errors == []


class TestEscapeHatch:
    """Test skip_mv_validation escape hatch."""

    def test_skip_true_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            skip_mv_validation="True",
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_skip_false_still_validates(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            skip_mv_validation="False",
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "missing MV_DECLARATIONS" in errors[0].message


class TestGrandfathering:
    """Test cutoff_date grandfathering."""

    def test_old_migration_exempt(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "old_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            create_date="2025-01-01 00:00:00.000000",
        )
        errors = validate_mv_migrations(versions, cutoff_date="2026-01-01")
        assert errors == []

    def test_new_migration_validated(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "new_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            create_date="2026-06-01 00:00:00.000000",
        )
        errors = validate_mv_migrations(versions, cutoff_date="2026-01-01")
        assert len(errors) == 1
        assert "missing MV_DECLARATIONS" in errors[0].message

    def test_no_cutoff_validates_all(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "old_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            create_date="2020-01-01 00:00:00.000000",
        )
        errors = validate_mv_migrations(versions)
        assert len(errors) == 1

    def test_mixed_old_and_new(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "old_mv",
            sql="CREATE MATERIALIZED VIEW {db}.old_mv TO {db}.old_dest AS SELECT 1 FROM {db}.src",
            create_date="2025-01-01 00:00:00.000000",
            revision="aaa000000001",
        )
        _write_migration(
            versions,
            "new_mv",
            sql="CREATE MATERIALIZED VIEW {db}.new_mv TO {db}.new_dest AS SELECT 1 FROM {db}.src",
            create_date="2026-06-01 00:00:00.000000",
            revision="bbb000000002",
            down_revision="aaa000000001",
        )
        errors = validate_mv_migrations(versions, cutoff_date="2026-01-01")
        assert len(errors) == 1
        assert errors[0].file.startswith("bbb")


class TestEmptyAndEdgeCases:
    """Test edge cases."""

    def test_empty_versions_dir(self, tmp_path: Path):
        versions = tmp_path / "versions"
        versions.mkdir()
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_non_python_files_ignored(self, tmp_path: Path):
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "README.md").write_text("# Migrations")
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_migration_without_mv_passes(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "add_column",
            sql="ALTER TABLE {db}.users ADD COLUMN phone String",
        )
        errors = validate_mv_migrations(versions)
        assert errors == []

    def test_error_includes_file_name(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
        )
        errors = validate_mv_migrations(versions)
        assert errors[0].file == "create_mv000_create_mv.py"

    def test_error_includes_mv_name(self, tmp_path: Path):
        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "my_mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"], "select_users": ["missing_reader"]}]'
            ),
        )
        errors = validate_mv_migrations(versions)
        assert errors[0].mv_name == "my_mv"


class TestSQLFileReferences:
    """Test that validation also reads referenced SQL files."""

    def test_mv_in_sql_file_detected(self, tmp_path: Path):
        versions = tmp_path / "versions"
        sql_dir = tmp_path / "sql" / "history" / "views" / "my_mv"
        sql_dir.mkdir(parents=True)

        sql_file = sql_dir / "2026_01_01_abc123.sql"
        sql_file.write_text(
            "CREATE MATERIALIZED VIEW {db}.my_mv TO {db}.dest AS SELECT 1 FROM {db}.src"
        )

        versions.mkdir(parents=True, exist_ok=True)
        rev_id = "abc123456789"
        content = textwrap.dedent(f"""\
            \"\"\"create my_mv

            Revision ID: {rev_id}
            Revises:
            Create Date: 2026-03-26 10:00:00.000000

            \"\"\"
            from alembic import op
            from ch_migrate import get_db, read_sql

            revision = '{rev_id}'
            down_revision = None
            branch_labels = None
            depends_on = None

            def upgrade() -> None:
                db = get_db()
                op.execute(read_sql("history/views/my_mv/2026_01_01_abc123.sql", db=db))

            def downgrade() -> None:
                pass
        """)
        (versions / f"{rev_id}_create_my_mv.py").write_text(content)

        errors = validate_mv_migrations(versions)
        assert len(errors) == 1
        assert "missing MV_DECLARATIONS" in errors[0].message


class TestCLIIntegration:
    """Test the up command's MV declaration preflight behavior."""

    def test_up_does_not_block_on_mv_warnings(self, tmp_path: Path, monkeypatch):
        from click.testing import CliRunner
        from ch_migrate.cli import main

        # Create minimal project structure
        (tmp_path / "config.yaml").write_text(textwrap.dedent("""\
            environments:
              dev:
                host: localhost
                database: testdb
                migration_user: test
        """))
        (tmp_path / ".env.local").write_text("CH_DEV_MIGRATION_PASSWORD=pass\n")

        versions = tmp_path / "migrations" / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
        )

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        monkeypatch.setattr("ch_migrate.gate.get_current_heads", lambda config: set())
        upgraded = runner.invoke(main, ["upgrade-env"], catch_exceptions=False)
        assert upgraded.exit_code == 0, upgraded.output
        result = runner.invoke(main, ["up", "dev"], catch_exceptions=False)
        assert result.exit_code != 0
        assert "script_location" in result.output  # reached Alembic after nonblocking preflight

    def test_up_skip_mv_check_bypasses(self, tmp_path: Path, monkeypatch):
        from click.testing import CliRunner
        from ch_migrate.cli import main

        (tmp_path / "config.yaml").write_text(textwrap.dedent("""\
            environments:
              dev:
                host: localhost
                database: testdb
                migration_user: test
        """))
        (tmp_path / ".env.local").write_text("CH_DEV_MIGRATION_PASSWORD=pass\n")

        versions = tmp_path / "migrations" / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
        )

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        # With --skip-mv-check, validation is bypassed before the env-version guard.
        result = runner.invoke(
            main, ["up", "dev", "--skip-mv-check"], catch_exceptions=False
        )
        # The validation finding must not be reported
        output = result.output
        assert "create_mv000_create_mv.py:" not in output
        assert "MV declaration validation failed" not in result.output
        assert "upgrade-env" in result.output


class TestLintIntegration:
    """Test MVDeclarationRule via lint_migrations."""

    def test_lint_catches_missing_declarations(self, tmp_path: Path):
        from ch_migrate.lint import LintConfig, Severity, lint_migrations

        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
        )
        report = lint_migrations(versions)
        mv_errors = [r for r in report.results if r.rule == "mv_declarations"]
        assert len(mv_errors) > 0
        assert mv_errors[0].severity == Severity.ERROR

    def test_lint_passes_with_declarations(self, tmp_path: Path):
        from ch_migrate.lint import lint_migrations

        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql=(
                "CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src;\n"
                "GRANT INSERT ON {db}.dest TO writer"
            ),
            mv_declarations=(
                '[{"mv_name": "mv", "source_table": "src", "target_table": "dest",'
                ' "inserting_users": ["writer"]}]'
            ),
        )
        report = lint_migrations(versions)
        mv_errors = [r for r in report.results if r.rule == "mv_declarations"]
        assert mv_errors == []

    def test_lint_respects_severity_off(self, tmp_path: Path):
        from ch_migrate.lint import LintConfig, Severity, lint_migrations

        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "create_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
        )
        config = LintConfig(rules={"mv_declarations": Severity.OFF})
        report = lint_migrations(versions, config=config)
        mv_errors = [r for r in report.results if r.rule == "mv_declarations"]
        assert mv_errors == []

    def test_lint_respects_cutoff(self, tmp_path: Path):
        from ch_migrate.lint import LintConfig, lint_migrations

        versions = tmp_path / "versions"
        _write_migration(
            versions,
            "old_mv",
            sql="CREATE MATERIALIZED VIEW {db}.mv TO {db}.dest AS SELECT 1 FROM {db}.src",
            create_date="2025-01-01 00:00:00.000000",
        )
        config = LintConfig(mv_validation_cutoff="2026-01-01")
        report = lint_migrations(versions, config=config)
        mv_errors = [r for r in report.results if r.rule == "mv_declarations"]
        assert mv_errors == []
