#!/bin/bash
# D2: async inserts buffered before the MV, flushed after. D3: can query_log QueryStart tell us which tables an in-flight insert touches?
q() { echo "> $1"; docker exec spike-rebuild-ch clickhouse-client --format PrettyCompactMonoBlock -q "$1"; }
q "DROP DATABASE IF EXISTS d2 SYNC"
q "CREATE DATABASE d2"
q "CREATE TABLE d2.t (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE TABLE d2.t_new (id UInt64) ENGINE = MergeTree ORDER BY id"
q "INSERT INTO d2.t SETTINGS async_insert = 1, wait_for_async_insert = 0, async_insert_use_adaptive_busy_timeout = 0, async_insert_busy_timeout_ms = 15000, async_insert_busy_timeout_max_ms = 15000 VALUES (1),(2),(3)"
q "SELECT database, table, first_update, total_bytes, length(entries.query_id) AS entries FROM system.asynchronous_inserts"
q "SELECT count() AS t_rows FROM d2.t"
echo "> [$(date +%T)] creating MV while async buffer pending"
q "CREATE MATERIALIZED VIEW d2.t_dual TO d2.t_new AS SELECT id FROM d2.t"
q "SYSTEM FLUSH ASYNC INSERT QUEUE"
q "SELECT (SELECT count() FROM d2.t) AS t_rows, (SELECT count() FROM d2.t_new) AS t_new_rows_via_mv"

echo; echo "=== D3: detection via query_log QueryStart.tables (cascade insert through an upstream MV) ==="
q "CREATE TABLE d2.feeder (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE MATERIALIZED VIEW d2.mv_feed TO d2.t AS SELECT id FROM d2.feeder"
SLOW="INSERT INTO d2.feeder SELECT number + 1000 FROM numbers(6000) WHERE sleepEachRow(0.001) = 0 SETTINGS max_block_size = 1000, min_insert_block_size_rows = 1000, min_insert_block_size_bytes = 0, max_threads = 1, function_sleep_max_microseconds_per_block = 10000000"
docker exec spike-rebuild-ch clickhouse-client --query_id d3-cascade -q "$SLOW" &
sleep 2
q "SYSTEM FLUSH LOGS"
q "SELECT type, query_id, tables, query_kind FROM system.query_log WHERE query_id = 'd3-cascade'"
q "SELECT query_id, query_kind, substr(query, 1, 30) q FROM system.processes WHERE query_kind = 'Insert'"
wait
q "SYSTEM FLUSH LOGS"
q "SELECT type, query_id, tables FROM system.query_log WHERE query_id = 'd3-cascade'"
