"""Write ch_migrate/access/privileges_26_3.json from a throwaway ClickHouse 26.3 container.

`access validate` checks privilege names offline against that file. This starts its own
`clickhouse/clickhouse-server:26.3` container, records the list with
`ch_migrate.access.bundle.record_privileges`, and removes the container, even on failure.
Review the diff before committing it. `tests/integration/test_access_privilege_list.py`
fails when the file and the image disagree.

    uv run --locked python scripts/bundle_privileges.py
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import clickhouse_connect

from ch_migrate.access.bundle import record_privileges
from ch_migrate.access.privileges import BUNDLED_LIST

IMAGE = "clickhouse/clickhouse-server:26.3"
SCRATCH_ROLE = "chm_privilege_list_scratch"


def main() -> int:
    name = "chm-privileges-" + secrets.token_hex(4)
    password = secrets.token_hex(16)
    try:
        port = _start_container(name, password)
        client = _connect(port, password)
        document = record_privileges(client, SCRATCH_ROLE)
        client.close()
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)
    BUNDLED_LIST.write_text(_format(document))
    count = len(document["privileges"])
    print(f"Wrote {count} privileges from {document['version']} to {_relative(BUNDLED_LIST)}")
    return 0


def _start_container(name: str, password: str) -> int:
    subprocess.run(
        ["docker", "run", "--detach", "--name", name, "--publish", "127.0.0.1::8123"]
        + ["--env", f"CLICKHOUSE_PASSWORD={password}"]
        + ["--env", "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1", IMAGE],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["docker", "port", name, "8123/tcp"], check=True, capture_output=True, text=True
    )
    return int(result.stdout.strip().rsplit(":", 1)[1])


def _connect(port: int, password: str):  # type: ignore[no-untyped-def]
    _wait_for_ping(port)
    return clickhouse_connect.get_client(
        host="127.0.0.1", port=port, username="default", password=password
    )


def _format(document: dict) -> str:
    """One privilege per line, so a new image's changes read as a short diff."""
    rows = ",\n".join("  " + json.dumps(row) for row in document["privileges"])
    return (
        f'{{\n "version": {json.dumps(document["version"])},\n "privileges": [\n{rows}\n ],\n'
        f' "source_spellings": {json.dumps(document["source_spellings"])}\n}}\n'
    )


def _wait_for_ping(port: int) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            with urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError("ClickHouse did not answer /ping within 60 seconds")
        time.sleep(0.5)


def _relative(path: Path) -> str:
    return str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd()) else str(path)


if __name__ == "__main__":
    sys.exit(main())
