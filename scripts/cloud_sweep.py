"""Drop integration-test leftovers on the ClickHouse Cloud test service.

A forced kill skips test finalizers, so `it_*` databases (and the
`<database>_migration` / `<database>_dict` users and roles some tests create) can remain. This
drops every `it_*` database that no query has touched for 24 hours, judged
from the query log of every replica, plus those users and roles.

Usage: export the same CH_MIGRATE_IT_URL the integration suite uses (load it
from the secret store; never print or commit it), then run

    uv run --locked python scripts/cloud_sweep.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

import clickhouse_connect

IDLE_HOURS = 24


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="list what would be dropped")
    args = parser.parse_args()
    url = os.environ.get("CH_MIGRATE_IT_URL")
    if not url:
        print("CH_MIGRATE_IT_URL is not set", file=sys.stderr)
        return 2
    client = connect(url)
    stale = stale_databases(client)
    for database in stale:
        print(("would drop " if args.dry_run else "dropping ") + database)
        if not args.dry_run:
            drop_database(client, database)
    print(f"{len(stale)} stale it_* database(s)")
    return 0


def connect(url: str):
    parsed = urlsplit(url)
    return clickhouse_connect.get_client(
        host=parsed.hostname,
        port=parsed.port or 8443,
        username=unquote(parsed.username or "default"),
        password=unquote(parsed.password or ""),
        secure=parsed.scheme == "https",
        connect_timeout=300,
        send_receive_timeout=300,
    )


def stale_databases(client) -> list[str]:
    """`it_*` databases with no query from any replica in the last IDLE_HOURS."""
    candidates = [
        row[0]
        for row in client.query(
            "SELECT name FROM system.databases WHERE startsWith(name, 'it_') ORDER BY name"
        ).result_rows
    ]
    if not candidates:
        return []
    active = {
        row[0]
        for row in client.query(
            "SELECT DISTINCT arrayJoin(databases) FROM clusterAllReplicas('default', "
            "merge('system', '^query_log')) WHERE event_time > now() - INTERVAL {hours:UInt32} HOUR",
            parameters={"hours": IDLE_HOURS},
            settings={"skip_unavailable_shards": 1},
        ).result_rows
    }
    return [name for name in candidates if name not in active]


def drop_database(client, database: str) -> None:
    client.command(f"DROP DATABASE IF EXISTS `{database}` SYNC")
    client.command(f"DROP USER IF EXISTS `{database}_migration`, `{database}_dict`")
    client.command(f"DROP ROLE IF EXISTS `{database}_migration_role`")


if __name__ == "__main__":
    sys.exit(main())
