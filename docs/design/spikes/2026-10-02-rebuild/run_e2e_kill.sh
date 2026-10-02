#!/bin/bash
# E (kill variant): SIGKILL the rebuild's python process during the 2nd partition's INSERT SELECT, then re-run.
set -u
cd "$(dirname "$0")"
chq() { docker exec spike-rebuild-ch clickhouse-client -q "$1"; }
hq() { curl -s 'http://127.0.0.1:18124/' --data-binary "$1"; }
rm -f STOP e2e_*.jsonl
uv run -q e2e.py setup
uv run -q e2e.py writer sync 10000000 e2e_w_sync.jsonl & WS=$!
uv run -q e2e.py writer async 50000000 e2e_w_async.jsonl & WA=$!
uv run -q e2e.py writer async0 90000000 e2e_w_async0.jsonl & WA0=$!
sleep 4
THROTTLE=1 uv run -q e2e.py rebuild e2e_rebuild.jsonl & RB=$!
for i in $(seq 1 3000); do
  st=$(hq "SELECT (SELECT count() FROM e.ledger WHERE step='copy' AND state='moved'), (SELECT max(written_rows) FROM system.processes WHERE query_id LIKE 'rebuild-stage-%')" 2>/dev/null)
  moved=$(echo "$st" | cut -f1); written=$(echo "$st" | cut -f2)
  if [ "${moved:-0}" -ge 1 ] && [ "${written:-0}" -ge 300000 ]; then
    echo "[$(date +%T)] SIGKILL rebuild python (moved=$moved, stage query written_rows=$written)"; pkill -9 -f "e2e.py rebuild"; break
  fi
  sleep 0.02
done
wait $RB 2>/dev/null
chq "SELECT 'right after kill: server-side stage query still running?', query_id, round(elapsed,2), written_rows FROM system.processes WHERE query_id LIKE 'rebuild-stage-%' FORMAT TSV"
chq "SELECT 'ledger', step, part, state, info FROM e.ledger ORDER BY at FORMAT TSV"
chq "SELECT 't_stage rows per partition', _partition_id, count() FROM e.t_stage GROUP BY 1 FORMAT TSV"
sleep 2
chq "SELECT 'after 2s: server-side stage query', query_id, round(elapsed,2), written_rows FROM system.processes WHERE query_id LIKE 'rebuild-stage-%' FORMAT TSV"
echo "[$(date +%T)] re-running rebuild"
THROTTLE=1 uv run -q e2e.py rebuild e2e_rebuild.jsonl
echo "[$(date +%T)] rebuild finished; writers keep going 3s"
sleep 3
touch STOP; wait $WS $WA $WA0
chq "SELECT 'ledger', step, part, state, info FROM e.ledger ORDER BY at FORMAT TSV"
chq "SELECT name, engine, toString(uuid) FROM system.tables WHERE database='e' ORDER BY name FORMAT TSV"
chq "SELECT 'final sorting key', sorting_key FROM system.tables WHERE database='e' AND name='t'"
uv run -q e2e.py verify e2e_w_sync.jsonl e2e_w_async.jsonl e2e_w_async0.jsonl e2e_rebuild.jsonl
chq "SYSTEM FLUSH LOGS"
chq "SELECT 'stage queries', query_id, type, query_duration_ms, written_rows, substr(exception,1,80) FROM system.query_log WHERE query_id LIKE 'rebuild-stage-%' AND type != 'QueryStart' ORDER BY event_time_microseconds FORMAT TSV"
chq "SELECT 'FlushError this run', count(), sum(bytes) FROM system.asynchronous_insert_log WHERE database='e' AND status='FlushError' AND event_time > now() - 600"
