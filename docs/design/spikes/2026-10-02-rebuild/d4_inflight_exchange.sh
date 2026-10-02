#!/bin/bash
# D4: an INSERT in flight across EXCHANGE TABLES and DROP of t_dual. Where do its rows land? Does EXCHANGE wait for it?
q() { echo "> $1"; docker exec spike-rebuild-ch clickhouse-client --format PrettyCompactMonoBlock -q "$1"; }
q "DROP DATABASE IF EXISTS d4 SYNC"
q "CREATE DATABASE d4"
q "CREATE TABLE d4.t (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE TABLE d4.t_new (id UInt64) ENGINE = MergeTree ORDER BY (intHash32(id), id)"
q "CREATE TABLE d4.sink (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE MATERIALIZED VIEW d4.mv_src TO d4.sink AS SELECT id FROM d4.t"
q "CREATE MATERIALIZED VIEW d4.t_dual TO d4.t_new AS SELECT id FROM d4.t"
q "SELECT name, uuid FROM system.tables WHERE database='d4' AND name IN ('t','t_new') ORDER BY name"
SLOW="INSERT INTO d4.t SELECT number FROM numbers(15000) WHERE sleepEachRow(0.001) = 0 SETTINGS max_block_size = 1000, min_insert_block_size_rows = 1000, min_insert_block_size_bytes = 0, max_threads = 1, function_sleep_max_microseconds_per_block = 10000000"
docker exec spike-rebuild-ch clickhouse-client --query_id d4-slow -q "$SLOW" &
sleep 4
q "SELECT (SELECT count() FROM d4.t) AS old_t_so_far"
q "EXCHANGE TABLES d4.t AND d4.t_new"
q "DROP TABLE d4.t_dual SYNC"
q "SELECT name, uuid FROM system.tables WHERE database='d4' AND name IN ('t','t_new') ORDER BY name"
wait
q "SYSTEM FLUSH LOGS"
q "SELECT query_duration_ms, substr(query,1,40) q FROM system.query_log WHERE type='QueryFinish' AND (query LIKE 'EXCHANGE TABLES d4%' OR query LIKE 'DROP TABLE d4.t_dual%')"
q "SELECT (SELECT count() FROM d4.t) AS new_physical_named_t, (SELECT uniqExact(id) FROM d4.t) AS new_t_uniq, (SELECT count() FROM d4.t_new) AS old_physical_named_t_new, (SELECT count() FROM d4.sink) AS sink_rows, (SELECT uniqExact(id) FROM d4.sink) AS sink_uniq"
