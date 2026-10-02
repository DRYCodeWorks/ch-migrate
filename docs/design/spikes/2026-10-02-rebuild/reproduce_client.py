"""Run archived clickhouse-client SQL through the fixture's local HTTP endpoint."""

import argparse
import sys
from pathlib import Path

import clickhouse_connect

from clickhouse_alembic.config import get_env_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--query", required=True)
    parser.add_argument("--query_id")
    parser.add_argument("--format", default="TabSeparated")
    args = parser.parse_args()
    config = get_env_config("it", Path("config.yaml"))
    if config["host"] not in ("127.0.0.1", "localhost"):
        raise SystemExit("This spike adapter only accepts an owned loopback fixture")
    client = clickhouse_connect.get_client(
        host=config["host"],
        port=config["port"],
        username=config["migration_user"],
        password=config["password"],
        secure=config.get("secure", False),
        send_receive_timeout=60,
    )
    try:
        settings = {"query_id": args.query_id} if args.query_id else None
        reads_result = args.query.lstrip().split(None, 1)[0].upper() in {
            "SELECT",
            "WITH",
            "SHOW",
            "DESCRIBE",
            "EXPLAIN",
        }
        output_format = args.format if reads_result else None
        sys.stdout.buffer.write(client.raw_query(args.query, settings=settings, fmt=output_format))
    finally:
        client.close()


if __name__ == "__main__":
    main()
