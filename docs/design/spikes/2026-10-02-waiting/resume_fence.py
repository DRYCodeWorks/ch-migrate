"""Recover only from the pre-send intent and server marker; never resubmit SQL."""

import time
from pathlib import Path

from ch_migrate.config import get_env_config
from ch_migrate.connection import get_client

config = get_env_config("it", Path("config.yaml"))
client = get_client(config)
database = config["database"]
try:
    token, expected_uuid = client.query(
        f"SELECT token, table_uuid FROM {database}.waiting_intent"
    ).result_rows[0]
    actual_uuid = client.query(
        "SELECT uuid FROM system.tables WHERE database = {db:String} AND name = 'counter'",
        parameters={"db": database},
    ).result_rows[0][0]
    assert str(actual_uuid) == expected_uuid
    markers = client.query(
        "SELECT mutation_id FROM system.mutations WHERE database = {db:String} "
        "AND table = 'counter' AND position(command, {token:String}) > 0",
        parameters={"db": database, "token": token},
    ).result_rows
    assert len(markers) == 1
    marker = markers[0][0]
    Path("resumed_marker").write_text(marker)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = client.query(
            "SELECT mutation_id, command, is_done FROM system.mutations "
            "WHERE database = {db:String} AND table = 'counter' ORDER BY mutation_id",
            parameters={"db": database},
        ).result_rows
        if all(row[2] for row in rows):
            value = client.command(f"SELECT x FROM {database}.counter")
            ids = {row[0] for row in rows}
            print(
                f"resumed {marker}; mutation_ids={sorted(ids)}; command_rows={len(rows)}; x={value}",
                flush=True,
            )
            assert value == 1 and ids == {marker} and len(rows) == 2
            break
        time.sleep(0.05)
    else:
        raise TimeoutError("Experiment did not finish")
finally:
    client.close()
