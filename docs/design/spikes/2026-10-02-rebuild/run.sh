#!/bin/bash
# run.sh file.sql -> executes with echo, writes file.out, prints it
f="$1"; out="${f%.sql}.out"
docker exec -i spike-rebuild-ch clickhouse-client --multiquery --echo --format PrettyCompactMonoBlock --ignore-error < "$f" > "$out" 2>&1
cat "$out"
