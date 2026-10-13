"""Does an insert racing EXCHANGE land only in the old storage (silently lost)?

Per trial: src (live `events`) with a dual MV src -> dst; writers insert continuously;
EXCHANGE events <-> dst; then every acknowledged id must be in the live `events`.
Also checks whether part_log identifies the stranded rows: NewPart in the old storage
after the swap from a query that made no part in the new storage.
"""

import sys
import threading
import time

import clickhouse_connect

PORT, MODE, TRIALS = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
ASYNC = {
    "async_insert": 1,
    "async_insert_use_adaptive_busy_timeout": 0,
    "async_insert_busy_timeout_ms": 5,
    "async_insert_busy_timeout_max_ms": 5,
}
SETTINGS = {
    "sync": {"async_insert": 0},
    "async0": {**ASYNC, "wait_for_async_insert": 0},
    "async1": {**ASYNC, "wait_for_async_insert": 1},
}[MODE]


def client():
    if PORT == 0:  # Cloud: CH_MIGRATE_IT_URL from the redacting runner.
        import os
        from urllib.parse import unquote, urlsplit

        url = urlsplit(os.environ["CH_MIGRATE_IT_URL"])
        return clickhouse_connect.get_client(
            host=url.hostname, port=url.port, username=url.username,
            password=unquote(url.password), secure=True,
        )
    return clickhouse_connect.get_client(host="127.0.0.1", port=PORT, password="probe")


def trial(admin, number):
    db = f"it_race_{number}"
    admin.command(f"DROP DATABASE IF EXISTS {db} SYNC")
    admin.command(f"CREATE DATABASE {db}")
    for name in ("events", "dst"):
        admin.command(f"CREATE TABLE {db}.{name} (id UInt64) ENGINE = MergeTree ORDER BY id")
    admin.command(f"CREATE MATERIALIZED VIEW {db}.dual TO {db}.dst AS SELECT id FROM {db}.events")
    uuids = dict(
        admin.query(
            f"SELECT name, toString(uuid) FROM system.tables WHERE database = '{db}'"
        ).result_rows
    )
    acked, errors, stop = [], [], threading.Event()

    def write(offset):
        own = client()
        next_id = offset
        while not stop.is_set():
            try:
                own.command(f"INSERT INTO {db}.events VALUES ({next_id})", settings=SETTINGS)
                acked.append(next_id)
            except Exception as exc:  # 741 for async1 is an honest rejection.
                errors.append(str(exc)[:80])
            next_id += 8

    threads = [threading.Thread(target=write, args=(k,)) for k in range(8)]
    for thread in threads:
        thread.start()
    time.sleep(0.15)
    swap_start = admin.command("SELECT toString(now64(6))")
    admin.command(f"EXCHANGE TABLES {db}.events AND {db}.dst")
    time.sleep(0.15)
    stop.set()
    for thread in threads:
        thread.join()
    time.sleep(0.3)  # Let 5 ms async timers fire.
    consistent = {"select_sequential_consistency": 1}
    live = {r[0] for r in admin.query(f"SELECT id FROM {db}.events", settings=consistent).result_rows}
    old = {r[0] for r in admin.query(f"SELECT id FROM {db}.dst", settings=consistent).result_rows}
    lost = set(acked) - live
    in_old_only = lost & old
    part_log = "clusterAllReplicas('default', system.part_log)" if PORT == 0 else "system.part_log"
    admin.command("SYSTEM FLUSH LOGS" + (" ON CLUSTER 'default'" if PORT == 0 else ""))
    stranded = admin.command(
        f"SELECT sum(rows) FROM {part_log} WHERE event_type = 'NewPart' "
        f"AND table_uuid = '{uuids['events']}' AND event_time_microseconds >= '{swap_start}' "
        f"AND query_id NOT IN (SELECT query_id FROM {part_log} WHERE event_type = 'NewPart' "
        f"AND table_uuid = '{uuids['dst']}')"
    )
    if int(stranded or 0) != len(in_old_only):
        print("MISMATCH trial", number, "stranded", stranded, "in_old_only", len(in_old_only))
    admin.command(f"DROP DATABASE {db} SYNC")
    return len(acked), len(lost), len(in_old_only), len(errors), errors[:1]


admin = client()
totals = [0, 0, 0, 0]
hits = 0
for number in range(TRIALS):
    *counts, sample = trial(admin, number)
    totals = [a + b for a, b in zip(totals, counts)]
    if counts[1]:
        hits += 1
        print("trial", number, "acked", counts[0], "lost", counts[1], "in_old_only", counts[2])
    if sample and number < 3:
        print("error sample", sample)
print(
    MODE, "trials", TRIALS, "with_loss", hits, "acked", totals[0], "lost", totals[1],
    "in_old_only", totals[2], "errors", totals[3],
)
