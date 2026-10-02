# /// script
# requires-python = ">=3.11"
# dependencies = ["clickhouse-connect>=0.8"]
# ///
"""End-to-end prototype of rebuild_table steps 1-5 with writers running.

Subcommands:
  setup                 create db e, table t (ORDER BY id), dependent MV, preload rows
  writer KIND START LOG write batches with increasing ids until STOP file exists
  rebuild LOG           run steps 1-5; every step checks state first, so a re-run resumes
  verify WLOG... RLOG   check every acked id is present, list duplicates vs the window
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta

import clickhouse_connect

DB = "e"
PRELOAD_PER_MONTH = 2_000_000
MONTHS = ["2026-07-01", "2026-08-01", "2026-09-01"]
STOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP")
COLS = "id UInt64, ts DateTime, k UInt8, payload String"


def client(**settings):
    return clickhouse_connect.get_client(host="127.0.0.1", port=18124, settings=settings)


def log(fh, **kv):
    kv["t"] = time.time()
    fh.write(json.dumps(kv) + "\n")
    fh.flush()


def setup():
    c = client()
    c.command(f"DROP DATABASE IF EXISTS {DB} SYNC")
    c.command(f"CREATE DATABASE {DB}")
    c.command(f"CREATE TABLE {DB}.t ({COLS}) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id")
    c.command(f"CREATE TABLE {DB}.sink (id UInt64) ENGINE = MergeTree ORDER BY id")
    c.command(f"CREATE MATERIALIZED VIEW {DB}.mv_dep TO {DB}.sink AS SELECT id FROM {DB}.t")
    c.command(f"CREATE TABLE {DB}.ledger (step String, part String, state String, info String, at DateTime64(6) DEFAULT now64(6)) ENGINE = MergeTree ORDER BY at")
    for i, m in enumerate(MONTHS):
        base = i * PRELOAD_PER_MONTH
        c.command(
            f"INSERT INTO {DB}.t SELECT {base} + number, toDateTime('{m}') + (number % (28*86400)), number % 7, repeat('p', 50) "
            f"FROM numbers({PRELOAD_PER_MONTH})"
        )
    print(c.query(f"SELECT partition_id, count(), sum(rows) FROM system.parts WHERE database='{DB}' AND table='t' AND active GROUP BY 1 ORDER BY 1").result_rows)


def writer(kind, start_id, logpath):
    settings = {"async_insert": 1, "wait_for_async_insert": 1, "async_insert_busy_timeout_ms": 200} if kind == "async" else {}
    c = client(**settings)
    batch = 2000 if kind == "sync" else 200
    next_id = start_id
    base = datetime(2026, 7, 1)
    with open(logpath, "w") as fh:
        while not os.path.exists(STOP):
            ids = range(next_id, next_id + batch)
            rows = [[i, base + timedelta(seconds=random.randrange(90 * 86400)), i % 7, "w"] for i in ids]
            t_send = time.time()
            for attempt in range(20):
                try:
                    c.insert(f"{DB}.t", rows, column_names=["id", "ts", "k", "payload"])
                    log(fh, kind=kind, first=ids[0], last=ids[-1], t_send=t_send, t_ack=time.time(), attempt=attempt)
                    break
                except Exception as exc:  # retry the same batch, like a real client
                    log(fh, kind=kind, first=ids[0], last=ids[-1], error=str(exc)[:200], attempt=attempt)
                    time.sleep(0.2)
            next_id += batch
            time.sleep(0.02)


def ledger_state(c, step, part=""):
    r = c.query(f"SELECT state, info FROM {DB}.ledger WHERE step=%(s)s AND part=%(p)s ORDER BY at DESC LIMIT 1", parameters={"s": step, "p": part}).result_rows
    return r[0] if r else (None, None)


def ledger_put(c, step, part, state, info=""):
    c.insert(f"{DB}.ledger", [[step, part, state, info]], column_names=["step", "part", "state", "info"])


def table_uuid(c, name):
    r = c.query(f"SELECT toString(uuid) FROM system.tables WHERE database='{DB}' AND name=%(n)s", parameters={"n": name}).result_rows
    return r[0][0] if r else None


def step1(c, fh):
    log(fh, step=1, phase="start")
    if not table_uuid(c, "t_new"):
        c.command(f"CREATE TABLE {DB}.t_new ({COLS}) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY (k, ts, id)")
    if ledger_state(c, "uuids")[0] is None:
        ledger_put(c, "uuids", "", "recorded", json.dumps({"old": table_uuid(c, "t"), "new": table_uuid(c, "t_new")}))
    if not table_uuid(c, "t_dual"):
        c.command(f"CREATE MATERIALIZED VIEW {DB}.t_dual TO {DB}.t_new AS SELECT id, ts, k, payload FROM {DB}.t")
    log(fh, step=1, phase="end")


def step2(c, fh):
    # The MV's creation time from the catalog (second precision; +1s margin). Any insert that started before it may
    # have built its view chain without t_dual.
    created = c.query(f"SELECT toUnixTimestamp(metadata_modification_time) FROM system.tables WHERE database='{DB}' AND name='t_dual'").result_rows[0][0]
    log(fh, step=2, phase="start", mv_created=created)
    while True:
        rows = c.query(
            "SELECT query_id, query_kind, elapsed, substr(query, 1, 60) FROM system.processes "
            "WHERE query_kind IN ('Insert', 'AsyncInsertFlush') AND toUnixTimestamp(now64(6) - toIntervalMillisecond(toUInt64(elapsed * 1000))) <= %(c)s + 1",
            parameters={"c": created},
        ).result_rows
        if not rows:
            break
        log(fh, step=2, waiting_on=[list(map(str, r)) for r in rows])
        time.sleep(0.2)
    log(fh, step=2, phase="end")


def step3(c, fh):
    log(fh, step=3, phase="start")
    if ledger_state(c, "snapshot")[0] == "done":
        log(fh, step=3, phase="skip")
        return
    c.command(f"DROP TABLE IF EXISTS {DB}.t_snap SYNC")
    c.command(f"CREATE TABLE {DB}.t_snap AS {DB}.t")
    parts = [r[0] for r in c.query(f"SELECT DISTINCT partition_id FROM system.parts WHERE database='{DB}' AND table='t' AND active ORDER BY 1").result_rows]
    for p in parts:
        c.command(f"ALTER TABLE {DB}.t_snap ATTACH PARTITION ID '{p}' FROM {DB}.t")
    counts = dict(c.query(f"SELECT _partition_id, count() FROM {DB}.t_snap GROUP BY 1").result_rows)
    ledger_put(c, "snapshot", "", "done", json.dumps(counts))
    log(fh, step=3, phase="end", parts=parts, counts=counts)


def step4(c, fh):
    log(fh, step=4, phase="start")
    if not table_uuid(c, "t_stage"):
        c.command(f"CREATE TABLE {DB}.t_stage AS {DB}.t_new")
    counts = json.loads(ledger_state(c, "snapshot")[1])
    for p, expected in sorted(counts.items()):
        state, _ = ledger_state(c, "copy", p)
        if state == "moved":
            continue
        staged = c.query(f"SELECT count() FROM {DB}.t_stage WHERE _partition_id = %(p)s", parameters={"p": p}).result_rows[0][0]
        if state == "staged" and staged == 0:
            # MOVE ran but the ledger write did not: the rows are already in t_new.
            ledger_put(c, "copy", p, "moved", "recovered")
            continue
        if state != "staged" or staged != expected:
            qid = f"rebuild-stage-{p}"
            c.command(f"KILL QUERY WHERE query_id = '{qid}' SYNC")
            c.command(f"ALTER TABLE {DB}.t_stage DROP PARTITION ID '{p}'")
            log(fh, step=4, part=p, phase="stage-start")
            c.command(
                f"INSERT INTO {DB}.t_stage SELECT * FROM {DB}.t_snap WHERE _partition_id = '{p}'",
                settings={"query_id": qid, "max_threads": 2, "max_insert_threads": 1},
            )
            staged = c.query(f"SELECT count() FROM {DB}.t_stage WHERE _partition_id = %(p)s", parameters={"p": p}).result_rows[0][0]
            assert staged == expected, (p, staged, expected)
            ledger_put(c, "copy", p, "staged", str(staged))
        c.command(f"ALTER TABLE {DB}.t_stage MOVE PARTITION ID '{p}' TO TABLE {DB}.t_new")
        ledger_put(c, "copy", p, "moved", str(expected))
        log(fh, step=4, part=p, phase="moved", rows=expected)
    log(fh, step=4, phase="end")


def step5(c, fh):
    log(fh, step=5, phase="start")
    uuids = json.loads(ledger_state(c, "uuids")[1])
    if table_uuid(c, "t") == uuids["old"]:
        c.command(f"EXCHANGE TABLES {DB}.t AND {DB}.t_new")
        log(fh, step=5, phase="exchanged")
    assert table_uuid(c, "t") == uuids["new"]
    # DROP ... SYNC returns only once in-flight inserts that captured t_dual have finished.
    c.command(f"DROP TABLE IF EXISTS {DB}.t_dual SYNC")
    log(fh, step=5, phase="t_dual_dropped")
    if table_uuid(c, "t_new") == uuids["old"]:
        c.command(f"RENAME TABLE {DB}.t_new TO {DB}.t_old_rebuilt")
    for tmp in ("t_snap", "t_stage"):
        c.command(f"DROP TABLE IF EXISTS {DB}.{tmp} SYNC")
    log(fh, step=5, phase="end")


def rebuild(logpath):
    c = client()
    with open(logpath, "a") as fh:
        for step in (step1, step2, step3, step4, step5):
            step(c, fh)


def verify(paths):
    c = client()
    events = [json.loads(line) for p in paths for line in open(p)]
    steps = {(e.get("step"), e.get("phase")): e["t"] for e in events if "step" in e and "phase" in e}
    w_start = min(t for (s, ph), t in steps.items() if s == 1 and ph == "start")
    w_end = max(t for (s, ph), t in steps.items() if s == 3 and ph == "end")
    batches = [e for e in events if "t_ack" in e]
    errors = [e for e in events if "error" in e]
    present = dict(c.query(f"SELECT id, count() FROM {DB}.t GROUP BY id").result_rows)
    sink = dict(c.query(f"SELECT id, count() FROM {DB}.sink GROUP BY id").result_rows)
    preload = len(MONTHS) * PRELOAD_PER_MONTH
    missing_pre = sum(1 for i in range(preload) if i not in present)
    dup_pre = sum(1 for i in range(preload) if present.get(i, 0) > 1)
    missing, dup_in, dup_out, acked = [], 0, [], 0
    for b in batches:
        for i in range(b["first"], b["last"] + 1):
            acked += 1
            n = present.get(i, 0)
            if n == 0:
                missing.append(i)
            elif n > 1:
                if b["t_send"] <= w_end and b["t_ack"] >= w_start:
                    dup_in += 1
                else:
                    dup_out.append((i, b["t_send"] - w_start, b["t_ack"] - w_start))
    sink_bad = sum(1 for i, n in sink.items() if n != 1)
    print(json.dumps({
        "preload_rows": preload, "preload_missing": missing_pre, "preload_duplicated": dup_pre,
        "writer_batches": len(batches), "writer_rows_acked": acked, "writer_errors_logged": len(errors),
        "missing_acked_ids": len(missing), "missing_sample": missing[:10],
        "duplicate_ids_inside_window": dup_in, "duplicate_ids_outside_window": len(dup_out), "outside_sample": dup_out[:10],
        "final_rows": sum(present.values()), "final_distinct_ids": len(present),
        "dependent_mv_sink_ids_not_exactly_once": sink_bad, "sink_rows": sum(sink.values()),
        "window_seconds": round(w_end - w_start, 3),
        "step_times_rel": {f"{s}:{ph}": round(t - w_start, 3) for (s, ph), t in sorted(steps.items(), key=lambda kv: kv[1])},
    }, indent=1))
    if errors:
        print("first errors:", errors[:3])


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "setup":
        setup()
    elif cmd == "writer":
        writer(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    elif cmd == "rebuild":
        rebuild(sys.argv[2])
    elif cmd == "verify":
        verify(sys.argv[2:])
