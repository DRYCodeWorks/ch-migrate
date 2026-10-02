SET send_logs_level = 'none';
DROP DATABASE IF EXISTS r1 SYNC; DROP DATABASE IF EXISTS r2 SYNC;
CREATE DATABASE r1; CREATE DATABASE r2;
-- Two replicas of each table on one server: same Keeper path, different replica names.
CREATE TABLE r1.t      (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/t', 'r1') PARTITION BY toYYYYMM(ts) ORDER BY id;
CREATE TABLE r2.t      (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/t', 'r2') PARTITION BY toYYYYMM(ts) ORDER BY id;
CREATE TABLE r1.t_snap (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/t_snap', 'r1') PARTITION BY toYYYYMM(ts) ORDER BY id;
CREATE TABLE r2.t_snap (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/t_snap', 'r2') PARTITION BY toYYYYMM(ts) ORDER BY id;
INSERT INTO r1.t SELECT number, toDateTime('2026-01-01 00:00:00') + intDiv(number, 50000) * 3600 * 24, repeat('x', 40) FROM numbers(5000000) SETTINGS max_partitions_per_insert_block = 1000;
SYSTEM SYNC REPLICA r2.t;
SELECT 'r2.t rows', count() FROM r2.t;
SYSTEM FLUSH LOGS;
SELECT 'fetches before', sum(ProfileEvents['ReplicatedPartFetches']) FROM system.query_log WHERE 0;
SELECT 'part_log before', event_type, table, database, count() FROM system.part_log WHERE database IN ('r1','r2') AND table='t_snap' GROUP BY ALL;
ALTER TABLE r1.t_snap ATTACH PARTITION 202601 FROM r1.t;
ALTER TABLE r1.t_snap ATTACH PARTITION 202602 FROM r1.t;
ALTER TABLE r1.t_snap ATTACH PARTITION 202603 FROM r1.t;
ALTER TABLE r1.t_snap ATTACH PARTITION 202604 FROM r1.t;
SYSTEM SYNC REPLICA r2.t_snap;
SELECT 'snap rows r1/r2', (SELECT count() FROM r1.t_snap), (SELECT count() FROM r2.t_snap);
SYSTEM FLUSH LOGS;
SELECT query_duration_ms, substr(query, 1, 60) AS q FROM system.query_log WHERE type='QueryFinish' AND query LIKE 'ALTER TABLE r1.t_snap ATTACH PARTITION%' ORDER BY event_time_microseconds;
-- How did replica r2 get the parts: cloned locally (hardlink from its own r2.t) or downloaded?
SELECT database, table, event_type, count() AS n, sum(rows) AS rows FROM system.part_log WHERE database IN ('r1','r2') AND table = 't_snap' GROUP BY ALL ORDER BY database, event_type;

-- MergeTree (non-replicated) -> ReplicatedMergeTree
CREATE TABLE r1.plain (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
INSERT INTO r1.plain VALUES (1, '2026-01-01', 'p');
ALTER TABLE r1.t_snap ATTACH PARTITION 202601 FROM r1.plain;
ALTER TABLE r1.plain ATTACH PARTITION 202601 FROM r1.t;
ALTER TABLE r1.plain MOVE PARTITION 202601 TO TABLE r1.t_snap;

-- MOVE PARTITION between Replicated tables
CREATE TABLE r1.stage (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/stage', 'r1') PARTITION BY toYYYYMM(ts) ORDER BY (payload, id);
CREATE TABLE r2.stage (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/stage', 'r2') PARTITION BY toYYYYMM(ts) ORDER BY (payload, id);
CREATE TABLE r1.t_new (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/t_new', 'r1') PARTITION BY toYYYYMM(ts) ORDER BY (payload, id);
CREATE TABLE r2.t_new (id UInt64, ts DateTime, payload String) ENGINE = ReplicatedMergeTree('/clickhouse/tables/spike/t_new', 'r2') PARTITION BY toYYYYMM(ts) ORDER BY (payload, id);
INSERT INTO r1.t_new VALUES (99999999, '2026-01-15', 'mv-row');
INSERT INTO r1.stage SELECT * FROM r1.t_snap WHERE _partition_id = '202601';
ALTER TABLE r1.stage MOVE PARTITION 202601 TO TABLE r1.t_new;
SYSTEM SYNC REPLICA r2.t_new; SYSTEM SYNC REPLICA r2.stage;
SELECT 'after replicated MOVE', (SELECT count() FROM r1.stage) s1, (SELECT count() FROM r2.stage) s2, (SELECT count() FROM r1.t_new) n1, (SELECT count() FROM r2.t_new) n2;
