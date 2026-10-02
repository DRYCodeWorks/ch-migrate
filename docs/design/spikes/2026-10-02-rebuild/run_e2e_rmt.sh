#!/bin/bash
# Orchestrates E: setup, writers, rebuild killed with SIGKILL mid-copy, re-run, verify.
set -u
cd "$(dirname "$0")"
chq() { docker exec spike-rebuild-ch clickhouse-client -q "$1"; }
rm -f STOP e2e_*.jsonl
ENGINE=ReplicatedMergeTree uv run -q e2e.py setup
uv run -q e2e.py writer sync 10000000 e2e_w_sync.jsonl & WS=$!
uv run -q e2e.py writer async 50000000 e2e_w_async.jsonl & WA=$!
uv run -q e2e.py writer async0 90000000 e2e_w_async0.jsonl & WA0=$!
sleep 4
ENGINE=ReplicatedMergeTree uv run -q e2e.py rebuild e2e_rebuild.jsonl & RB=$!
for i in $(seq 1 0); do
  moved=$(chq "SELECT count() FROM e.ledger WHERE step='copy' AND state='moved'" 2>/dev/null || echo 0)
  running=$(chq "SELECT count() FROM system.processes WHERE query_id LIKE 'rebuild-stage-%'" 2>/dev/null || echo 0)
  if [ "${moved:-0}" -ge 1 ] && [ "${running:-0}" -ge 1 ]; then
    echo "[$(date +%T)] SIGKILL rebuild (moved=$moved, stage query running)"; pkill -9 -f "e2e.py rebuild"; break
  fi
  sleep 0.05
done
wait $RB 2>/dev/null
chq "SELECT 'after kill: server-side stage query', query_id, round(elapsed,2), written_rows FROM system.processes WHERE query_id LIKE 'rebuild-stage-%'"
chq "SELECT 'ledger', step, part, state, info FROM e.ledger ORDER BY at FORMAT TSV"
sleep 1
chq "SELECT 'after 1s: server-side stage query', query_id, round(elapsed,2), written_rows FROM system.processes WHERE query_id LIKE 'rebuild-stage-%'"
echo "[$(date +%T)] re-running rebuild"
ENGINE=ReplicatedMergeTree uv run -q e2e.py rebuild e2e_rebuild.jsonl
echo "[$(date +%T)] rebuild finished; writers keep going 3s"
sleep 3
touch STOP; wait $WS $WA $WA0
chq "SELECT 'ledger', step, part, state, info FROM e.ledger ORDER BY at FORMAT TSV"
chq "SELECT name, engine, toString(uuid) FROM system.tables WHERE database='e' ORDER BY name FORMAT TSV"
chq "SELECT 'final sorting key', sorting_key FROM system.tables WHERE database='e' AND name='t'"
uv run -q e2e.py verify e2e_w_sync.jsonl e2e_w_async.jsonl e2e_w_async0.jsonl e2e_rebuild.jsonl
grep -h waiting_on e2e_rebuild.jsonl | head -3
chq "SYSTEM FLUSH LOGS"
chq "SELECT 'async_insert_log', status, count(), sum(rows), any(substr(exception,1,90)) FROM system.asynchronous_insert_log WHERE database='e' GROUP BY status FORMAT TSV"
chq "SELECT 'stage queries', query_id, type, query_duration_ms, written_rows, substr(exception,1,80) FROM system.query_log WHERE query_id LIKE 'rebuild-stage-%' AND type != 'QueryStart' ORDER BY event_time_microseconds FORMAT TSV"
