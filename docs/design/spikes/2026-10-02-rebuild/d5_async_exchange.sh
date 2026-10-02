#!/bin/bash
# D5: async-insert entries queued against the old table before EXCHANGE TABLES, flushed after it.
q() { echo "> $1"; docker exec spike-rebuild-ch clickhouse-client --format PrettyCompactMonoBlock -q "$1"; }
A="SETTINGS async_insert = 1, async_insert_use_adaptive_busy_timeout = 0, async_insert_busy_timeout_ms = 8000, async_insert_busy_timeout_max_ms = 8000"
q "DROP DATABASE IF EXISTS d5 SYNC"
q "CREATE DATABASE d5"
q "CREATE TABLE d5.t (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE TABLE d5.t_new (id UInt64) ENGINE = MergeTree ORDER BY (intHash32(id), id)"
q "CREATE MATERIALIZED VIEW d5.t_dual TO d5.t_new AS SELECT id FROM d5.t"
q "INSERT INTO d5.t $A, wait_for_async_insert = 0 VALUES (1),(2),(3)"
echo "> [background] INSERT INTO d5.t $A, wait_for_async_insert = 1 VALUES (4),(5)"
docker exec spike-rebuild-ch clickhouse-client -q "INSERT INTO d5.t $A, wait_for_async_insert = 1 VALUES (4),(5)" > d5_wait1_client.txt 2>&1 &
sleep 1
q "SELECT database, table, total_bytes, length(entries.query_id) entries FROM system.asynchronous_inserts WHERE database='d5'"
q "EXCHANGE TABLES d5.t AND d5.t_new"
q "SYSTEM FLUSH ASYNC INSERT QUEUE"
wait
echo "> client of the wait_for_async_insert=1 insert saw:"; cat d5_wait1_client.txt
q "SELECT (SELECT groupArray(id) FROM d5.t) AS name_t_new_physical, (SELECT groupArray(id) FROM d5.t_new) AS name_t_new_old_physical"
q "SYSTEM FLUSH LOGS"
q "SELECT status, rows, substr(exception, 1, 110) AS exception FROM system.asynchronous_insert_log WHERE database = 'd5' ORDER BY event_time_microseconds"

echo; echo "=== D5c: is an in-progress AsyncInsertFlush visible in system.processes? ==="
q "CREATE TABLE d5.slow_src (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE TABLE d5.slow_dst (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE MATERIALIZED VIEW d5.slow_mv TO d5.slow_dst AS SELECT id FROM d5.slow_src WHERE sleepEachRow(0.5) = 0"
q "INSERT INTO d5.slow_src $A, wait_for_async_insert = 0 VALUES (1),(2),(3),(4),(5),(6)"
docker exec spike-rebuild-ch clickhouse-client -q "SYSTEM FLUSH ASYNC INSERT QUEUE" &
sleep 1
q "SELECT query_kind, round(elapsed, 2) AS elapsed, is_initial_query, substr(query, 1, 50) AS q FROM system.processes WHERE query_kind != 'Select'"
wait
q "SELECT count() FROM d5.slow_dst"
