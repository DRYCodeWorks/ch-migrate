"""Submit one owned ALTER over HTTP without ever reading its response."""

import http.client
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from clickhouse_alembic.config import get_env_config

config = get_env_config("it", Path("config.yaml"))
token = sys.argv[1]
query = (
    f"ALTER TABLE {config['database']}.counter UPDATE x = x + 1 WHERE 1, "
    f"DELETE WHERE 0 AND '{token}' = '{token}'"
)
connection_type = (
    http.client.HTTPSConnection if config.get("secure") else http.client.HTTPConnection
)
connection = connection_type(config["host"], config["port"], timeout=30)
parameters = urlencode({"mutations_sync": 0, "alter_sync": 0, "query_id": token})
connection.request(
    "POST",
    "/?" + parameters,
    body=query.encode(),
    headers={"X-ClickHouse-User": config["migration_user"], "X-ClickHouse-Key": config["password"]},
)
# Deliberately never call getresponse() or write a mutation-id receipt.
Path("submitted_without_ack").write_text("sent")
time.sleep(60)
