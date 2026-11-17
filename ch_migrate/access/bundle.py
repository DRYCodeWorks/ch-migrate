"""Record the bundled privilege list from a server, for `scripts/bundle_privileges.py`.

`system.privileges` gives the names, aliases, levels and groups. It doesn't mark which
privileges are data sources, and ClickHouse 25.8 and later store an old source spelling such
as `REMOTE ON *.*` as `READ` and `WRITE` on that source. So each global privilege is granted
on `*.*` to a scratch role, and those stored that way are recorded as old spellings.

This is a maintainer tool: it writes only to the scratch role it is given, and drops it.
`tests/integration/test_access_privilege_list.py` runs it to pin the bundled file.
"""

from __future__ import annotations

from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError

from ch_migrate.access.templates import statement

# The access types an old source spelling is stored as.
_SOURCE_ACCESS = frozenset({"READ", "WRITE"})


def record_privileges(client: Any, scratch_role: str) -> dict[str, Any]:
    """The privilege list in the bundled file's shape, read from the server `client` reaches."""
    rows = client.query(statement("bundle", "privileges")).named_results()
    privileges = [
        {
            "privilege": row["privilege"],
            "aliases": list(row["aliases"]),
            "level": row["level"],
            "parent_group": row["parent_group"],
        }
        for row in rows
    ]
    return {
        "version": client.server_version,
        "privileges": privileges,
        "source_spellings": _source_spellings(client, privileges, scratch_role),
    }


def _source_spellings(client: Any, privileges: list[dict], role: str) -> dict[str, str]:
    """Each global privilege the server stores as READ and WRITE, and the source it means."""
    spellings = {}
    client.command(statement("bundle", "create_scratch_role", {"role": role}))
    try:
        for row in privileges:
            if row["level"] == "GLOBAL":
                source = _stored_source(client, row["privilege"], role)
                if source is not None:
                    spellings[row["privilege"]] = source
    finally:
        client.command(statement("bundle", "drop_scratch_role", {"role": role}))
    return spellings


def _stored_source(client: Any, privilege: str, role: str) -> str | None:
    """The source `<privilege> ON *.*` is stored as ("" for every source), or None."""
    client.command(statement("bundle", "clear_scratch_role", {"role": role}))
    values = {"privilege": privilege, "role": role}
    try:
        client.command(statement("bundle", "grant_on_everything", values))
    except DatabaseError:
        return None  # a privilege the admin can't pass on is no source spelling it can check
    result = client.query(statement("bundle", "scratch_grants"), parameters={"role": role})
    stored = {(access, source) for access, source in result.result_rows}
    if not stored or {access for access, _ in stored} - _SOURCE_ACCESS:
        return None
    return str(min(source for _, source in stored))
