#!/bin/bash
# D1: does an INSERT that started before CREATE MATERIALIZED VIEW push rows through the new MV?
q() { echo "> $1"; docker exec spike-rebuild-ch clickhouse-client --format PrettyCompactMonoBlock -q "$1"; }
q "DROP DATABASE IF EXISTS d SYNC"
q "CREATE DATABASE d"
q "CREATE TABLE d.t (id UInt64) ENGINE = MergeTree ORDER BY id"
q "CREATE TABLE d.t_new (id UInt64) ENGINE = MergeTree ORDER BY id"
SLOW="INSERT INTO d.t SELECT number FROM numbers(20000) WHERE sleepEachRow(0.001) = 0 SETTINGS max_block_size = 1000, min_insert_block_size_rows = 1000, min_insert_block_size_bytes = 0, max_threads = 1, function_sleep_max_microseconds_per_block = 10000000"
echo "> [background, $(date +%T)] $SLOW"
docker exec spike-rebuild-ch clickhouse-client --query_id d1-slow -q "$SLOW" &
sleep 5
q "SELECT query_id, query_kind, current_database, round(elapsed,1) AS elapsed, now() - toIntervalSecond(toUInt64(elapsed)) AS approx_start, written_rows, substr(query,1,40) AS q FROM system.processes WHERE query_kind = 'Insert'"
q "SELECT count() AS t_rows_so_far FROM d.t"
echo "> [$(date +%T)] creating MV"
q "CREATE MATERIALIZED VIEW d.t_dual TO d.t_new AS SELECT id FROM d.t"
sleep 3
q "SELECT count() AS t_rows_so_far FROM d.t"
wait
echo "> [$(date +%T)] slow insert finished"
q "SELECT (SELECT count() FROM d.t) AS t_rows, (SELECT count() FROM d.t_new) AS t_new_rows_via_mv"
q "INSERT INTO d.t SELECT number + 100000 FROM numbers(10)"
q "SELECT (SELECT count() FROM d.t) AS t_rows, (SELECT count() FROM d.t_new) AS t_new_rows_via_mv"
