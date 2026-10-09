"""Materialized view declaration validation for ch-migrate.

When a migration creates a MATERIALIZED VIEW, ClickHouse 24.2+ requires the
inserting user to have INSERT on the target table. Without it, MVs silently
fail — no errors, data simply doesn't land. This module enforces that migrations
declare their MV dependencies and include the required companion grants.

Usage in migrations:

    MV_DECLARATIONS = [{
        "mv_name": "my_new_mv",
        "source_table": "logs",
        "target_table": "my_new_aggregate",
        "inserting_users": ["intake_writer"],
        # Optional fields for additional validation:
        "select_users": ["readonly_user"],
        "source_rls_users": ["intake_writer"],
        "target_rls_policies": ["default_deny", "full_access"],
    }]

Escape hatch:

    # skip_mv_validation: MV is created by a different mechanism (e.g. replicated from another cluster)
    skip_mv_validation = True
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class MVDeclaration:
    """A single materialized view declaration from a migration."""

    mv_name: str
    source_table: str
    target_table: str
    inserting_users: list[str]
    # Optional validation fields
    select_users: list[str] = field(default_factory=list)
    source_rls_users: list[str] = field(default_factory=list)
    target_rls_policies: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MVDeclaration:
        return cls(
            mv_name=d.get("mv_name", ""),
            source_table=d.get("source_table", ""),
            target_table=d.get("target_table", ""),
            inserting_users=d.get("inserting_users", []),
            select_users=d.get("select_users", []),
            source_rls_users=d.get("source_rls_users", []),
            target_rls_policies=d.get("target_rls_policies", []),
        )


@dataclass
class MVValidationError:
    """A validation error found during MV declaration checking."""

    file: str
    message: str
    mv_name: str | None = None


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _load_module_attribute(path: Path, attr_name: str) -> Any:
    """Safely extract a module-level attribute from a Python file using AST.

    Returns None if the attribute is not found or cannot be parsed.
    """
    try:
        content = path.read_text()
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == attr_name:
                    try:
                        return ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        return None
    return None


def _extract_create_date(path: Path) -> str | None:
    """Extract Create Date from a migration file's docstring header."""
    content = path.read_text()
    match = re.search(r"^Create Date:\s*(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# SQL pattern matching
# ---------------------------------------------------------------------------

# Detects CREATE MATERIALIZED VIEW (with or without IF NOT EXISTS)
_RE_CREATE_MV = re.compile(
    r"\bCREATE\s+MATERIALIZED\s+VIEW\b",
    re.IGNORECASE,
)

# Matches GRANT INSERT ON [db.]table TO user1[, user2, ...]
# Handles: {db}.table, db.table, table (no db prefix)
# Handles backtick-quoted identifiers and comma-separated user lists
_RE_GRANT_INSERT = re.compile(
    r"GRANT\s+INSERT\s+ON\s+"
    r"(?:(?:\{[^}]*\}|`?\w+`?)\.)?`?(\w+)`?"
    r"\s+TO\s+(.+?)(?:;|\n|\Z)",
    re.IGNORECASE,
)

# Matches GRANT SELECT ON [db.]table TO user1[, user2, ...]
_RE_GRANT_SELECT = re.compile(
    r"GRANT\s+SELECT(?:\([^)]*\))?\s+ON\s+"
    r"(?:(?:\{[^}]*\}|`?\w+`?)\.)?`?(\w+)`?"
    r"\s+TO\s+(.+?)(?:;|\n|\Z)",
    re.IGNORECASE,
)

# Matches CREATE ROW POLICY ... ON [db.]table
# Captures: (policy_name_fragment, table_name)
_RE_CREATE_ROW_POLICY = re.compile(
    r"CREATE\s+ROW\s+POLICY\s+(?:IF\s+NOT\s+EXISTS\s+|OR\s+REPLACE\s+)?"
    r"(\S+)\s+ON\s+(?:(?:\{[^}]*\}|`?\w+`?)\.)?`?(\w+)`?",
    re.IGNORECASE,
)

# Matches ROW POLICY with USING 1 (permissive) — for source RLS checks
_RE_ROW_POLICY_PERMISSIVE = re.compile(
    r"CREATE\s+ROW\s+POLICY\s+(?:IF\s+NOT\s+EXISTS\s+|OR\s+REPLACE\s+)?"
    r"\S+\s+ON\s+(?:(?:\{[^}]*\}|`?\w+`?)\.)?`?(\w+)`?"
    r"[^;]*?USING\s+1\s+TO\s+(.+?)(?:;|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _sql_has_create_mv(sql: str) -> bool:
    """Check if SQL contains a CREATE MATERIALIZED VIEW statement."""
    return bool(_RE_CREATE_MV.search(sql))


def _parse_user_list(users_str: str) -> list[str]:
    """Parse a comma-separated user list from a GRANT statement.

    Handles backtick-quoted identifiers and strips whitespace.
    """
    return [
        u.strip().strip("`").lower()
        for u in users_str.split(",")
        if u.strip().strip("`")
    ]


def _find_grant_inserts(content: str) -> list[tuple[str, str]]:
    """Find all GRANT INSERT ON table TO user patterns.

    Returns list of (table_name, user_name) tuples, lowercased.
    Handles comma-separated user lists (e.g., TO user_a, user_b).
    """
    results: list[tuple[str, str]] = []
    for m in _RE_GRANT_INSERT.finditer(content):
        table = m.group(1).lower()
        for user in _parse_user_list(m.group(2)):
            results.append((table, user))
    return results


def _find_grant_selects(content: str) -> list[tuple[str, str]]:
    """Find all GRANT SELECT ON table TO user patterns.

    Returns list of (table_name, user_name) tuples, lowercased.
    Handles comma-separated user lists (e.g., TO user_a, user_b).
    """
    results: list[tuple[str, str]] = []
    for m in _RE_GRANT_SELECT.finditer(content):
        table = m.group(1).lower()
        for user in _parse_user_list(m.group(2)):
            results.append((table, user))
    return results


def _find_row_policies(content: str) -> list[tuple[str, str]]:
    """Find all CREATE ROW POLICY ... ON table patterns.

    Returns list of (policy_name, table_name) tuples, lowercased.
    """
    return [
        (m.group(1).lower(), m.group(2).lower())
        for m in _RE_CREATE_ROW_POLICY.finditer(content)
    ]


def _find_permissive_row_policies(content: str) -> list[tuple[str, str]]:
    """Find ROW POLICY ... ON table USING 1 TO user patterns.

    Returns list of (table_name, user_or_users_string) tuples.
    """
    return [
        (m.group(1).lower(), m.group(2).lower().strip())
        for m in _RE_ROW_POLICY_PERMISSIVE.finditer(content)
    ]


# ---------------------------------------------------------------------------
# Migration SQL extraction
# ---------------------------------------------------------------------------


def _read_migration_sql_from_content(content: str, path: Path) -> str:
    """Extract SQL from migration file content.

    Extracts SQL string literals from op.execute() calls and read_sql()/run_sql()
    file references. Falls back to the full file content if no SQL is found.

    Args:
        content: The migration file's text content.
        path: The migration file path (used to resolve read_sql references).
    """
    sql_parts: list[str] = []

    for match in re.finditer(
        r'op\.execute\(\s*(?:f?"""(.*?)"""|f?"([^"]*)")', content, re.DOTALL
    ):
        sql_parts.append(match.group(1) or match.group(2) or "")

    for match in re.finditer(r'(?:read_sql|run_sql)\(\s*["\']([^"\']+)["\']', content):
        sql_path = path.parent.parent / "sql" / match.group(1)
        if sql_path.exists():
            sql_parts.append(sql_path.read_text())

    return "\n".join(sql_parts) if sql_parts else content


def _read_migration_sql(path: Path) -> str:
    """Extract SQL from op.execute() calls and read_sql()/run_sql() files.

    Convenience wrapper that reads the file and delegates to
    _read_migration_sql_from_content.
    """
    return _read_migration_sql_from_content(path.read_text(), path)


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------


def _validate_required_fields(
    decl: dict[str, Any], file_name: str
) -> tuple[list[MVValidationError], MVDeclaration | None]:
    """Validate required fields in an MV declaration dict.

    Returns (errors, parsed_declaration). If errors, declaration may be None.
    """
    errors: list[MVValidationError] = []

    mv_name = decl.get("mv_name")
    if not mv_name:
        errors.append(
            MVValidationError(
                file=file_name,
                message="MV_DECLARATIONS entry missing required field 'mv_name'",
            )
        )
        return errors, None

    for required in ("source_table", "target_table", "inserting_users"):
        if required not in decl or not decl[required]:
            errors.append(
                MVValidationError(
                    file=file_name,
                    mv_name=mv_name,
                    message=f"MV_DECLARATIONS['{mv_name}'] missing required field '{required}'",
                )
            )

    if errors:
        return errors, None

    return errors, MVDeclaration.from_dict(decl)


def _check_grant_inserts(
    decl: MVDeclaration,
    grant_inserts: list[tuple[str, str]],
    file_name: str,
) -> list[MVValidationError]:
    """Check GRANT INSERT ON target_table TO user for each inserting_user."""
    errors: list[MVValidationError] = []
    target = decl.target_table.lower()

    for user in decl.inserting_users:
        has_grant = any(
            table == target and u == user.lower() for table, u in grant_inserts
        )
        if not has_grant:
            errors.append(
                MVValidationError(
                    file=file_name,
                    mv_name=decl.mv_name,
                    message=(
                        f"Missing: GRANT INSERT ON {{db}}.{decl.target_table} "
                        f"TO {user}"
                    ),
                )
            )

    return errors


def _check_grant_selects(
    decl: MVDeclaration,
    grant_selects: list[tuple[str, str]],
    file_name: str,
) -> list[MVValidationError]:
    """Check GRANT SELECT ON target_table TO user for each select_user."""
    errors: list[MVValidationError] = []
    target = decl.target_table.lower()

    for user in decl.select_users:
        has_grant = any(
            table == target and u == user.lower() for table, u in grant_selects
        )
        if not has_grant:
            errors.append(
                MVValidationError(
                    file=file_name,
                    mv_name=decl.mv_name,
                    message=(
                        f"Missing: GRANT SELECT ON {{db}}.{decl.target_table} "
                        f"TO {user}"
                    ),
                )
            )

    return errors


def _check_source_rls(
    decl: MVDeclaration,
    permissive_policies: list[tuple[str, str]],
    file_name: str,
) -> list[MVValidationError]:
    """Check CREATE ROW POLICY ON source_table USING 1 TO user."""
    errors: list[MVValidationError] = []
    source = decl.source_table.lower()

    for user in decl.source_rls_users:
        has_policy = any(
            table == source
            and user.lower() in [u.strip() for u in users_str.split(",")]
            for table, users_str in permissive_policies
        )
        if not has_policy:
            errors.append(
                MVValidationError(
                    file=file_name,
                    mv_name=decl.mv_name,
                    message=(
                        f"Missing: CREATE ROW POLICY ... ON {{db}}.{decl.source_table} "
                        f"USING 1 TO {user} (permissive policy for MV source reads)"
                    ),
                )
            )

    return errors


def _check_target_rls(
    decl: MVDeclaration,
    row_policies: list[tuple[str, str]],
    file_name: str,
) -> list[MVValidationError]:
    """Check that named RLS policies exist on the target table."""
    errors: list[MVValidationError] = []
    target = decl.target_table.lower()

    for policy_name in decl.target_rls_policies:
        has_policy = any(
            table == target and policy_name.lower() in policy
            for policy, table in row_policies
        )
        if not has_policy:
            errors.append(
                MVValidationError(
                    file=file_name,
                    mv_name=decl.mv_name,
                    message=(
                        f"Missing: CREATE ROW POLICY containing '{policy_name}' "
                        f"ON {{db}}.{decl.target_table}"
                    ),
                )
            )

    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_mv_migrations(
    versions_dir: Path,
    *,
    cutoff_date: str | None = None,
) -> list[MVValidationError]:
    """Validate all migrations with CREATE MATERIALIZED VIEW.

    Checks that migrations creating MVs include MV_DECLARATIONS and that
    the required companion grants exist in the migration batch (all .py files
    in versions_dir).

    Args:
        versions_dir: Path to migrations/versions/ directory.
        cutoff_date: Optional date string (YYYY-MM-DD or full timestamp).
            Migrations with Create Date before this are exempt (grandfathering).

    Returns:
        List of validation errors. Empty list means all checks passed.
    """
    errors: list[MVValidationError] = []
    migration_files = sorted(versions_dir.glob("*.py"))

    if not migration_files:
        return errors

    # Read each file once and cache results.
    file_cache: dict[Path, tuple[str, str]] = {}  # path -> (raw_content, extracted_sql)
    batch_content_parts: list[str] = []
    for path in migration_files:
        raw = path.read_text()
        sql = _read_migration_sql_from_content(raw, path)
        file_cache[path] = (raw, sql)
        batch_content_parts.append(raw)
        if sql:
            batch_content_parts.append(sql)

    batch_content = "\n".join(batch_content_parts)

    # Pre-compute batch-level indexes for grant/policy checks
    grant_inserts = _find_grant_inserts(batch_content)
    grant_selects = _find_grant_selects(batch_content)
    row_policies = _find_row_policies(batch_content)
    permissive_policies = _find_permissive_row_policies(batch_content)

    # Validate each migration that creates an MV
    for path in migration_files:
        _, sql = file_cache[path]
        if not sql or not _sql_has_create_mv(sql):
            continue

        file_name = path.name

        # Grandfathering: skip migrations before cutoff date
        if cutoff_date:
            create_date = _extract_create_date(path)
            if create_date and create_date < cutoff_date:
                continue

        # Escape hatch
        skip = _load_module_attribute(path, "skip_mv_validation")
        if skip is True:
            continue

        # Require MV_DECLARATIONS
        declarations = _load_module_attribute(path, "MV_DECLARATIONS")
        if declarations is None:
            errors.append(
                MVValidationError(
                    file=file_name,
                    message=(
                        "Migration creates a MATERIALIZED VIEW but is missing "
                        "MV_DECLARATIONS. Add MV_DECLARATIONS = [...] or "
                        "skip_mv_validation = True (with a comment explaining why)."
                    ),
                )
            )
            continue

        if not isinstance(declarations, list):
            errors.append(
                MVValidationError(
                    file=file_name,
                    message="MV_DECLARATIONS must be a list of dicts.",
                )
            )
            continue

        if not declarations:
            errors.append(
                MVValidationError(
                    file=file_name,
                    message="MV_DECLARATIONS is empty.",
                )
            )
            continue

        # Validate each declaration
        for decl_dict in declarations:
            if not isinstance(decl_dict, dict):
                errors.append(
                    MVValidationError(
                        file=file_name,
                        message="Each MV_DECLARATIONS entry must be a dict.",
                    )
                )
                continue

            field_errors, decl = _validate_required_fields(decl_dict, file_name)
            errors.extend(field_errors)
            if decl is None:
                continue

            # Core check: GRANT INSERT on target for each inserting user
            errors.extend(
                _check_grant_inserts(decl, grant_inserts, file_name)
            )

            # Optional checks (only if the declaration includes them)
            if decl.select_users:
                errors.extend(
                    _check_grant_selects(decl, grant_selects, file_name)
                )

            if decl.source_rls_users:
                errors.extend(
                    _check_source_rls(decl, permissive_policies, file_name)
                )

            if decl.target_rls_policies:
                errors.extend(
                    _check_target_rls(decl, row_policies, file_name)
                )

    return errors
