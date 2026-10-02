SET send_logs_level = 'none', async_insert = 0;
DROP DATABASE IF EXISTS f SYNC;
CREATE DATABASE f;
SELECT name, value FROM system.settings WHERE name IN ('deduplicate_blocks_in_dependent_materialized_views', 'insert_deduplicate', 'materialized_views_ignore_errors');
SELECT name, value FROM system.merge_tree_settings WHERE name IN ('parts_to_throw_insert', 'parts_to_delay_insert', 'replicated_deduplication_window', 'non_replicated_deduplication_window');
-- Replicated: t_new rejects id 42 until the constraint is dropped (stands in for any transient MV-side failure, e.g. TOO_MANY_PARTS on t_new)
CREATE TABLE f.t (id UInt64) ENGINE = ReplicatedMergeTree('/clickhouse/tables/f/t', 'r1') ORDER BY id;
CREATE TABLE f.t_new (id UInt64, CONSTRAINT c CHECK id != 42) ENGINE = ReplicatedMergeTree('/clickhouse/tables/f/t_new', 'r1') ORDER BY id;
CREATE MATERIALIZED VIEW f.t_dual TO f.t_new AS SELECT id FROM f.t;
INSERT INTO f.t VALUES (41), (42);
SELECT 'after failed attempt', (SELECT groupArray(id) FROM f.t) AS t, (SELECT groupArray(id) FROM f.t_new) AS t_new;
ALTER TABLE f.t_new DROP CONSTRAINT c;
INSERT INTO f.t VALUES (41), (42);
SELECT 'after client retry', (SELECT groupArray(id) FROM f.t) AS t, (SELECT groupArray(id) FROM f.t_new) AS t_new;
-- same with deduplicate_blocks_in_dependent_materialized_views = 1
CREATE TABLE f.t2 (id UInt64) ENGINE = ReplicatedMergeTree('/clickhouse/tables/f/t2', 'r1') ORDER BY id;
CREATE TABLE f.t2_new (id UInt64, CONSTRAINT c CHECK id != 42) ENGINE = ReplicatedMergeTree('/clickhouse/tables/f/t2_new', 'r1') ORDER BY id;
CREATE MATERIALIZED VIEW f.t2_dual TO f.t2_new AS SELECT id FROM f.t2;
INSERT INTO f.t2 SETTINGS deduplicate_blocks_in_dependent_materialized_views = 1 VALUES (41), (42);
ALTER TABLE f.t2_new DROP CONSTRAINT c;
INSERT INTO f.t2 SETTINGS deduplicate_blocks_in_dependent_materialized_views = 1 VALUES (41), (42);
SELECT 'dedup_in_mv=1 after retry', (SELECT groupArray(id) FROM f.t2) AS t, (SELECT groupArray(id) FROM f.t2_new) AS t_new;
-- Non-replicated MergeTree (no insert dedup by default)
CREATE TABLE f.m (id UInt64) ENGINE = MergeTree ORDER BY id;
CREATE TABLE f.m_new (id UInt64, CONSTRAINT c CHECK id != 42) ENGINE = MergeTree ORDER BY id;
CREATE MATERIALIZED VIEW f.m_dual TO f.m_new AS SELECT id FROM f.m;
INSERT INTO f.m VALUES (41), (42);
ALTER TABLE f.m_new DROP CONSTRAINT c;
INSERT INTO f.m VALUES (41), (42);
SELECT 'MergeTree after retry', (SELECT groupArray(id) FROM f.m) AS t, (SELECT groupArray(id) FROM f.m_new) AS t_new;
