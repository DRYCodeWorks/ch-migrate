DROP DATABASE IF EXISTS b SYNC;
CREATE DATABASE b;
CREATE TABLE b.t (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
INSERT INTO b.t SELECT number, toDateTime('2026-01-01 00:00:00') + intDiv(number, 50000) * 3600 * 24, repeat('x', 40) FROM numbers(5000000) SETTINGS max_partitions_per_insert_block = 1000;
OPTIMIZE TABLE b.t FINAL;
SELECT partition, count() AS parts, sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS size FROM system.parts WHERE database='b' AND table='t' AND active GROUP BY partition ORDER BY partition;

-- t_snap: same structure as t
CREATE TABLE b.t_snap AS b.t;
SET send_logs_level = 'none';
ALTER TABLE b.t_snap ATTACH PARTITION 202601 FROM b.t;
ALTER TABLE b.t_snap ATTACH PARTITION 202602 FROM b.t;
ALTER TABLE b.t_snap ATTACH PARTITION 202603 FROM b.t;
ALTER TABLE b.t_snap ATTACH PARTITION 202604 FROM b.t;
SYSTEM FLUSH LOGS;
SELECT query_duration_ms, read_rows, written_rows, ProfileEvents['WriteBufferFromFileDescriptorWriteBytes'] AS bytes_written, substr(query, 1, 60) AS q FROM system.query_log WHERE type='QueryFinish' AND query LIKE 'ALTER TABLE b.t_snap ATTACH PARTITION%' ORDER BY event_time_microseconds;
SELECT count() FROM b.t_snap;
-- hardlinks? compare inode of a data file in t and t_snap
SELECT table, name, path FROM system.parts WHERE database='b' AND table IN ('t','t_snap') AND active AND partition='202601';

-- structure mismatches
CREATE TABLE b.diff_order (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY (payload, id);
ALTER TABLE b.diff_order ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_partkey (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMMDD(ts) ORDER BY id;
ALTER TABLE b.diff_partkey ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_coltype (id UInt64, ts DateTime, payload LowCardinality(String)) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
ALTER TABLE b.diff_coltype ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_colorder (id UInt64, payload String, ts DateTime) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
ALTER TABLE b.diff_colorder ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_extracol (id UInt64, ts DateTime, payload String, extra UInt8 DEFAULT 0) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
ALTER TABLE b.diff_extracol ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_index (id UInt64, ts DateTime, payload String, INDEX ip payload TYPE bloom_filter GRANULARITY 1) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
ALTER TABLE b.diff_index ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_settings (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id SETTINGS index_granularity = 1024;
ALTER TABLE b.diff_settings ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_engine (id UInt64, ts DateTime, payload String) ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
ALTER TABLE b.diff_engine ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_ttl (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id TTL ts + INTERVAL 10 YEAR;
ALTER TABLE b.diff_ttl ATTACH PARTITION 202601 FROM b.t;
CREATE TABLE b.diff_codec (id UInt64 CODEC(Delta, ZSTD), ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
ALTER TABLE b.diff_codec ATTACH PARTITION 202601 FROM b.t;
SELECT 'diff_*' AS t, table, sum(rows) FROM system.parts WHERE database='b' AND table LIKE 'diff_%' AND active GROUP BY table ORDER BY table;
