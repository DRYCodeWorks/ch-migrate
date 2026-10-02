"""Explicit local experiments for the waiting design; not auto-collected."""

import secrets
import subprocess
import sys
import time
from pathlib import Path

import clickhouse_connect
import pytest

pytestmark = pytest.mark.integration


def test_waiting_metadata_and_mutation_markers(project):
    for table in ("mutations", "distributed_ddl_queue", "query_log", "part_log"):
        columns = project.client.query(
            "SELECT name, type FROM system.columns WHERE database = 'system' AND table = {table:String} "
            "AND (position(name, 'query') > 0 OR position(name, 'mutation') > 0 OR name IN ('command', 'entry', 'settings')) "
            "ORDER BY position",
            parameters={"table": table},
        ).result_rows
        print(table, columns)
    target = f"{project.database}.ownership"
    project.client.command(
        f"CREATE TABLE {target} (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {target} VALUES (1, 0), (2, 0)")
    project.client.command(f"SYSTEM STOP MERGES {target}")
    try:
        statements = [
            f"ALTER TABLE {target} UPDATE x = x + 1 WHERE id = 1 /* chm_marker_comment */",
            f"ALTER TABLE {target} UPDATE x = x + 1 WHERE id = 2 AND 'chm_marker_expression' = 'chm_marker_expression'",
        ]
        for sql in statements:
            query_id = "chm_probe_" + secrets.token_hex(8)
            project.client.command(
                sql, settings={"mutations_sync": 0, "query_id": query_id, "log_comment": query_id}
            )
            print("submitted", query_id)
        rows = project.client.query(
            "SELECT mutation_id, command, is_done FROM system.mutations "
            "WHERE database = {db:String} AND table = 'ownership' ORDER BY mutation_id",
            parameters={"db": project.database},
        ).result_rows
        print("mutation commands", rows)
        project.client.command("SYSTEM FLUSH LOGS")
        events = project.client.query(
            "SELECT query_id, type, log_comment, query FROM system.query_log "
            "WHERE startsWith(query_id, 'chm_probe_') AND position(query, {db:String}) > 0 "
            "ORDER BY event_time_microseconds",
            parameters={"db": project.database},
        ).result_rows
        profiles = project.client.query(
            "SELECT query_id, mapFilter((k, v) -> positionCaseInsensitive(k, 'mutation') > 0, ProfileEvents) "
            "FROM system.query_log WHERE type = 'QueryFinish' AND startsWith(query_id, 'chm_probe_') "
            "AND position(query, {db:String}) > 0",
            parameters={"db": project.database},
        ).result_rows
        print("mutation profile events", profiles)
        print("submission events", events)
    finally:
        project.client.command(f"SYSTEM START MERGES {target}")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        values = project.client.query(f"SELECT id, x FROM {target} ORDER BY id").result_rows
        if values == [(1, 1), (2, 1)]:
            break
        time.sleep(0.05)
    assert values == [(1, 1), (2, 1)]


def test_waiting_schema_mutation_marker_options(project, clickhouse_server, request):
    clauses = [
        "MODIFY COLUMN x UInt32, DELETE WHERE 0 AND 'chm_schema_marker' = 'chm_schema_marker'",
        "MATERIALIZE COLUMN y, DELETE WHERE 0 AND 'chm_schema_marker' = 'chm_schema_marker'",
        "CLEAR COLUMN y, DELETE WHERE 0 AND 'chm_schema_marker' = 'chm_schema_marker'",
        "RENAME COLUMN y TO renamed, DELETE WHERE 0 AND 'chm_schema_marker' = 'chm_schema_marker'",
    ]
    for index, clause in enumerate(clauses):
        name = f"schema_probe_{index}"
        table = f"{project.database}.{name}"
        project.client.command(
            f"CREATE TABLE {table} (id UInt64, x UInt64, y UInt64 DEFAULT x + 1) "
            "ENGINE = MergeTree ORDER BY id"
        )
        project.client.command(f"INSERT INTO {table} (id, x) VALUES (1, 10), (2, 20)")
        project.client.command(f"SYSTEM STOP MERGES {table}")
        server = clickhouse_server
        worker = clickhouse_connect.get_client(
            host=server.host,
            port=server.port,
            username=server.user,
            password=server.password,
            secure=server.secure,
            send_receive_timeout=2,
        )
        request.addfinalizer(worker.close)
        query_id = "chm_schema_" + secrets.token_hex(8)
        try:
            try:
                worker.command(
                    f"ALTER TABLE {table} {clause}",
                    settings={"mutations_sync": 0, "alter_sync": 0, "query_id": query_id},
                )
                print("combined marker request", clause, "returned")
            except Exception as error:
                print(
                    "combined marker request",
                    clause,
                    type(error).__name__,
                    str(error).splitlines()[0],
                )
        finally:
            project.client.command(f"SYSTEM START MERGES {table}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            active = project.client.query(
                "SELECT count() FROM system.processes WHERE query_id = {id:String}",
                parameters={"id": query_id},
            ).result_rows[0][0]
            if not active:
                break
            time.sleep(0.05)
        result = project.client.query(
            "SELECT mutation_id, command, is_done FROM system.mutations WHERE database = {db:String} AND table = {table:String}",
            parameters={"db": project.database, "table": name},
        ).result_rows
        print("combined marker durable commands", clause, result)


def test_waiting_ddl_settings_identity(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    marker = "chm_ddl_" + secrets.token_hex(8)
    cluster.stop_node(2)
    try:
        cluster.clients[1].command(
            f"CREATE TABLE {project.database}.ddl_owned ON CLUSTER {cluster.name} (id UInt64) ENGINE = Memory",
            settings={
                "distributed_ddl_task_timeout": 0,
                "distributed_ddl_output_mode": "none",
                "log_comment": marker,
            },
        )
        rows = (
            cluster.clients[1]
            .query(
                "SELECT entry, host, query, settings['log_comment'], status "
                "FROM system.distributed_ddl_queue WHERE position(query, {db:String}) > 0 "
                "AND position(query, 'ddl_owned') > 0 ORDER BY host",
                parameters={"db": project.database},
            )
            .result_rows
        )
        print("DDL durable marker", marker, rows)
        assert len(rows) == 2 and all(row[3] == marker for row in rows)
    finally:
        cluster.start_node(2)


def test_waiting_actual_mutation_corpus(project):
    cases = [
        ("update", "ALTER TABLE {table} UPDATE x = x + 1 WHERE id = 1"),
        ("delete", "ALTER TABLE {table} DELETE WHERE id = 1"),
        ("lightweight_delete", "DELETE FROM {table} WHERE id = 1"),
        ("lightweight_update", "UPDATE {table} SET x = x + 1 WHERE id = 1"),
        ("modify_type", "ALTER TABLE {table} MODIFY COLUMN x UInt32"),
        ("drop_column", "ALTER TABLE {table} DROP COLUMN y"),
        ("clear_column", "ALTER TABLE {table} CLEAR COLUMN y"),
        ("materialize_column", "ALTER TABLE {table} MATERIALIZE COLUMN y"),
        ("rename_column", "ALTER TABLE {table} RENAME COLUMN y TO z"),
        ("modify_ttl", "ALTER TABLE {table} MODIFY TTL ts + INTERVAL 1 DAY"),
        ("failure", "ALTER TABLE {table} UPDATE x = throwIf(x = 5) WHERE 1"),
    ]
    for label, template in cases:
        name = "corpus_" + label
        table = f"{project.database}.{name}"
        project.client.command(
            f"CREATE TABLE {table} (id UInt64, x UInt64, y UInt64 DEFAULT x + 1, ts DateTime DEFAULT now()) "
            "ENGINE = MergeTree ORDER BY id"
        )
        project.client.command(f"INSERT INTO {table} (id, x) VALUES (1, 1), (5, 5)")
        settings = {"mutations_sync": 0, "alter_sync": 0, "lightweight_deletes_sync": 0}
        if label == "lightweight_update":
            settings["allow_experimental_lightweight_update"] = 1
        try:
            project.client.command(template.format(table=table), settings=settings)
            outcome = "accepted"
        except Exception as error:
            outcome = type(error).__name__ + ": " + str(error).splitlines()[0]
        deadline = time.monotonic() + 5
        while True:
            mutations = project.client.query(
                "SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason "
                "FROM system.mutations WHERE database = {db:String} AND table = {table:String}",
                parameters={"db": project.database, "table": name},
            ).result_rows
            if (
                not mutations
                or all(row[2] or row[4] for row in mutations)
                or time.monotonic() > deadline
            ):
                break
            time.sleep(0.05)
        print("mutation corpus", label, outcome, mutations)


def test_waiting_single_node_deduplicated_intent(project):
    table = f"{project.database}.intents"
    project.client.command(
        f"CREATE TABLE {table} (step String, owner String) ENGINE = MergeTree ORDER BY step "
        "SETTINGS non_replicated_deduplication_window = 10000"
    )
    for owner in ("first", "second"):
        project.client.insert(
            table,
            [["step1", owner]],
            column_names=["step", "owner"],
            settings={"insert_deduplication_token": "same-step", "insert_deduplicate": 1},
        )
    rows = project.client.query(f"SELECT step, owner FROM {table}").result_rows
    print("single intent dedup", rows)
    assert rows == [("step1", "first")]


def test_waiting_cluster_deduplicated_intent(cluster_project, clickhouse_cluster):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.intents"
    cluster.clients[1].command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (step String, owner String) "
        f"ENGINE = ReplicatedMergeTree('/clickhouse/intents/{project.database}', '{{replica}}') ORDER BY step"
    )
    barrier = Barrier(2)

    def insert_owner(node):
        barrier.wait()
        cluster.clients[node].insert(
            table,
            [["step1", f"node{node}"]],
            column_names=["step", "owner"],
            settings={
                "insert_deduplication_token": "same-step",
                "insert_deduplicate": 1,
                "insert_quorum": "auto",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(insert_owner, (1, 2)))
    for node, client in cluster.clients.items():
        client.command(f"SYSTEM SYNC REPLICA {table} LIGHTWEIGHT")
        rows = client.query(f"SELECT step, owner FROM {table}").result_rows
        print("cluster intent dedup", node, rows)
        assert len(rows) == 1 and rows[0][1] in ("node1", "node2")


def test_waiting_lightweight_update_with_required_storage(project):
    table = f"{project.database}.patch_probe"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id "
        "SETTINGS enable_block_number_column = 1, enable_block_offset_column = 1"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 10), (2, 20)")
    project.client.command(
        f"UPDATE {table} SET x = x + 1 WHERE id = 1",
        settings={"allow_experimental_lightweight_update": 1, "mutations_sync": 0},
    )
    mutations = project.client.query(
        "SELECT mutation_id, command, is_done FROM system.mutations WHERE database = {db:String} AND table = 'patch_probe'",
        parameters={"db": project.database},
    ).result_rows
    values = project.client.query(f"SELECT id, x FROM {table} ORDER BY id").result_rows
    print("lightweight update enabled", mutations, values)
    assert values == [(1, 11), (2, 20)]


def test_waiting_empty_fence_cost_and_retention(project):
    table = f"{project.database}.fence_cost"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64, payload String) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(
        f"INSERT INTO {table} SELECT number, number, repeat('payload', 100) FROM numbers(10000)"
    )
    project.client.command(
        f"ALTER TABLE {table} DELETE WHERE 0 AND 'chm_cost_marker' = 'chm_cost_marker'",
        settings={"mutations_sync": 2},
    )
    project.client.command("SYSTEM FLUSH LOGS")
    names = {
        row[0]
        for row in project.client.query(
            "SELECT name FROM system.columns WHERE database = 'system' AND table = 'part_log'"
        ).result_rows
    }
    fields = [
        name
        for name in ("event_type", "query_id", "rows", "read_rows", "read_bytes", "ProfileEvents")
        if name in names
    ]
    if fields:
        events = project.client.query(
            "SELECT " + ", ".join(fields) + " FROM system.part_log "
            "WHERE database = {db:String} AND table = 'fence_cost' AND toString(event_type) LIKE '%Mutat%'",
            parameters={"db": project.database},
        ).result_rows
        print("empty fence physical cost", fields, events)
    mutations = project.client.query(
        "SELECT command, is_done, parts_to_do FROM system.mutations WHERE database = {db:String} AND table = 'fence_cost'",
        parameters={"db": project.database},
    ).result_rows
    print("empty fence retained", mutations)
    assert project.client.command(f"SELECT count() FROM {table}") == 10000


@pytest.mark.parametrize("trial", range(3))
def test_waiting_kill_before_receipt_and_resume_from_intent(project, trial):
    table = f"{project.database}.counter"
    project.client.command(f"CREATE TABLE {table} (x UInt64) ENGINE = MergeTree ORDER BY tuple()")
    project.client.command(f"INSERT INTO {table} VALUES (0)")
    table_uuid = str(
        project.client.query(
            "SELECT uuid FROM system.tables WHERE database = {db:String} AND name = 'counter'",
            parameters={"db": project.database},
        ).result_rows[0][0]
    )
    project.client.command(
        f"CREATE TABLE {project.database}.waiting_intent (token String, table_uuid String) "
        "ENGINE = MergeTree ORDER BY token SETTINGS non_replicated_deduplication_window = 10000"
    )
    token = "chm_recovery_" + secrets.token_hex(8)
    project.client.insert(
        f"{project.database}.waiting_intent",
        [[token, table_uuid]],
        column_names=["token", "table_uuid"],
        settings={"insert_deduplication_token": "revision-step", "insert_deduplicate": 1},
    )
    project.client.command(f"SYSTEM STOP MERGES {table}")
    scripts = Path(__file__).parents[2] / "docs/design/spikes/2026-10-02-waiting"
    submitted = subprocess.Popen(
        [sys.executable, str(scripts / "submit_without_ack.py"), token],
        cwd=project.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    resumed = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = project.client.query(
                "SELECT mutation_id, command, is_done FROM system.mutations "
                "WHERE database = {db:String} AND table = 'counter' ORDER BY mutation_id",
                parameters={"db": project.database},
            ).result_rows
            if any(token in row[1] for row in rows):
                break
            time.sleep(0.05)
        assert len(rows) == 2 and any(token in row[1] for row in rows), rows
        original_ids = [row[0] for row in rows]
        print("pre-receipt crash", trial, rows)
        submitted.kill()
        submitted.communicate(timeout=5)
        resumed = subprocess.Popen(
            [sys.executable, str(scripts / "resume_fence.py")],
            cwd=project.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not (project.root / "resumed_marker").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (project.root / "resumed_marker").exists()
        assert resumed.poll() is None
        project.client.command(f"SYSTEM START MERGES {table}")
        output = resumed.communicate(timeout=20)[0]
        assert resumed.returncode == 0, output
        print("recovered", trial, output.strip())
        ids = [
            row[0]
            for row in project.client.query(
                "SELECT mutation_id FROM system.mutations WHERE database = {db:String} "
                "AND table = 'counter' ORDER BY mutation_id",
                parameters={"db": project.database},
            ).result_rows
        ]
        assert ids == original_ids
        assert project.client.command(f"SELECT x FROM {table}") == 1
    finally:
        for process in (submitted, resumed):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        project.client.command(f"SYSTEM START MERGES {table}")


def test_waiting_foreign_failure_blocks_later_mutation(project):
    table = f"{project.database}.foreign_probe"
    project.client.command(
        f"CREATE TABLE {table} (id UInt64, x UInt64) ENGINE = MergeTree ORDER BY id"
    )
    project.client.command(f"INSERT INTO {table} VALUES (1, 1), (5, 5)")
    project.client.command(
        f"ALTER TABLE {table} UPDATE x = throwIf(x = 5) WHERE 1", settings={"mutations_sync": 0}
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = project.client.query(
            "SELECT mutation_id, command, is_done, latest_fail_reason FROM system.mutations "
            "WHERE database = {db:String} AND table = 'foreign_probe' ORDER BY mutation_id",
            parameters={"db": project.database},
        ).result_rows
        if rows and rows[0][3]:
            break
        time.sleep(0.05)
    assert rows[0][3]
    project.client.command(
        f"ALTER TABLE {table} UPDATE x = x + 1 WHERE id = 1", settings={"mutations_sync": 0}
    )
    rows = project.client.query(
        "SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason FROM system.mutations "
        "WHERE database = {db:String} AND table = 'foreign_probe' ORDER BY mutation_id",
        parameters={"db": project.database},
    ).result_rows
    print("foreign failure and queued follower", rows)
    assert len(rows) == 2 and all(not row[2] for row in rows)
    assert project.client.query(f"SELECT id, x FROM {table} ORDER BY id").result_rows == [
        (1, 1),
        (5, 5),
    ]


def test_waiting_requires_every_replica(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.replica_wait"
    cluster.clients[1].command(
        f"CREATE TABLE {table} ON CLUSTER {cluster.name} (id UInt64, x UInt64) "
        f"ENGINE = ReplicatedMergeTree('/clickhouse/wait/{project.database}', '{{replica}}') ORDER BY id"
    )
    cluster.clients[1].command(f"INSERT INTO {table} VALUES (1, 0)")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
        cluster.clients[2].command(f"SYSTEM STOP {action} {table}")
    try:
        cluster.clients[1].command(
            f"ALTER TABLE {table} UPDATE x = x + 1 WHERE 1", settings={"mutations_sync": 0}
        )
        cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table} PULL")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            states = {}
            for node, client in cluster.clients.items():
                states[node] = client.query(
                    "SELECT mutation_id, is_done, parts_to_do FROM system.mutations "
                    "WHERE database = {db:String} AND table = 'replica_wait'",
                    parameters={"db": project.database},
                ).result_rows
            if states[1] and states[2] and states[1][0][1] and not states[2][0][1]:
                break
            time.sleep(0.05)
        print("replica completion divergence", states)
        assert states[1][0][1] == 1 and states[2][0][1] == 0
        assert states[1][0][0] == states[2][0][0]
    finally:
        for action in ("MERGES", "FETCHES", "REPLICATION QUEUES"):
            cluster.clients[2].command(f"SYSTEM START {action} {table}")
    cluster.clients[2].command(f"SYSTEM SYNC REPLICA {table}")
    assert cluster.clients[2].query(f"SELECT x FROM {table}").result_rows == [(1,)]


def test_waiting_managed_sync_settings_override(project):
    table = f"{project.database}.sync_settings"
    project.client.command(f"CREATE TABLE {table} (x UInt64) ENGINE = MergeTree ORDER BY tuple()")
    project.client.command(f"INSERT INTO {table} VALUES (0)")
    project.client.command(f"SYSTEM STOP MERGES {table}")
    try:
        project.client.command(
            f"ALTER TABLE {table} UPDATE x = x + 1 WHERE 1, "
            "DELETE WHERE 0 AND 'chm_settings_marker' = 'chm_settings_marker' "
            "SETTINGS mutations_sync = 2, mutations_sync = 0, alter_sync = 0"
        )
        rows = project.client.query(
            "SELECT mutation_id, command, is_done FROM system.mutations "
            "WHERE database = {db:String} AND table = 'sync_settings'",
            parameters={"db": project.database},
        ).result_rows
        print("managed sync override", rows)
        assert rows and all(row[2] == 0 for row in rows)
    finally:
        project.client.command(f"SYSTEM START MERGES {table}")


def test_waiting_completed_ownership_marker_can_expire(project):
    table = f"{project.database}.retention_probe"
    project.client.command(
        f"CREATE TABLE {table} (x UInt64) ENGINE = MergeTree ORDER BY tuple() "
        "SETTINGS finished_mutations_to_keep = 1, old_parts_lifetime = 0, "
        "cleanup_delay_period = 1, cleanup_delay_period_random_add = 0"
    )
    project.client.command(f"INSERT INTO {table} VALUES (0)")
    project.client.command(
        f"ALTER TABLE {table} UPDATE x = x + 1 WHERE " "'chm_expired_owner' = 'chm_expired_owner'",
        settings={"mutations_sync": 2},
    )
    project.client.command(
        f"ALTER TABLE {table} DELETE WHERE 0",
        settings={"mutations_sync": 2},
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = project.client.query(
            "SELECT mutation_id, command FROM system.mutations "
            "WHERE database = {db:String} AND table = 'retention_probe'",
            parameters={"db": project.database},
        ).result_rows
        if not any("chm_expired_owner" in row[1] for row in rows):
            break
        time.sleep(0.1)
    print("expired ownership marker", rows, "x =", project.client.command(f"SELECT x FROM {table}"))
    assert rows and not any("chm_expired_owner" in row[1] for row in rows)
    assert project.client.command(f"SELECT x FROM {table}") == 1


def test_waiting_ddl_failure_names_host(cluster_project, clickhouse_cluster):
    project, cluster = cluster_project, clickhouse_cluster
    table = f"{project.database}.ddl_failure"
    for client in cluster.clients.values():
        client.command(f"CREATE TABLE {table} (id UInt64, x String) ENGINE = MergeTree ORDER BY id")
    cluster.clients[2].command(f"ALTER TABLE {table} ADD COLUMN conflict String")
    token = "chm_ddl_failure_" + secrets.token_hex(8)
    cluster.clients[1].command(
        f"ALTER TABLE {table} ON CLUSTER {cluster.name} ADD COLUMN conflict UInt64",
        settings={
            "distributed_ddl_task_timeout": 0,
            "distributed_ddl_output_mode": "none",
            "log_comment": token,
        },
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = (
            cluster.clients[1]
            .query(
                "SELECT host, status, exception_code, exception_text FROM system.distributed_ddl_queue "
                "WHERE settings['log_comment'] = {token:String} ORDER BY host",
                parameters={"token": token},
            )
            .result_rows
        )
        if len(rows) == 2 and any(row[2] for row in rows):
            break
        time.sleep(0.05)
    print("DDL host exception", rows)
    assert len(rows) == 2 and any(row[2] and row[3] for row in rows)
