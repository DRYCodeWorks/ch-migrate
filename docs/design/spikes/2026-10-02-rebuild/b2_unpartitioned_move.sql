SET send_logs_level = 'none';
-- Unpartitioned (PARTITION BY tuple())
CREATE TABLE b.u (id UInt64, v String) ENGINE = MergeTree ORDER BY id;
INSERT INTO b.u SELECT number, toString(number) FROM numbers(1000);
INSERT INTO b.u SELECT number, toString(number) FROM numbers(1000, 1000);
SELECT partition, partition_id, count() FROM system.parts WHERE database='b' AND table='u' AND active GROUP BY ALL;
CREATE TABLE b.u_snap AS b.u;
ALTER TABLE b.u_snap ATTACH PARTITION tuple() FROM b.u;
SELECT 'u_snap after tuple()', count() FROM b.u_snap;
CREATE TABLE b.u_snap2 AS b.u;
ALTER TABLE b.u_snap2 ATTACH PARTITION ID 'all' FROM b.u;
SELECT 'u_snap2 after ID all', count() FROM b.u_snap2;
CREATE TABLE b.u_snap3 AS b.u;
ALTER TABLE b.u_snap3 ATTACH PARTITION ALL FROM b.u;
SELECT 'u_snap3 after ALL', count() FROM b.u_snap3;
-- ALL on a partitioned table
CREATE TABLE b.t_all AS b.t;
ALTER TABLE b.t_all ATTACH PARTITION ALL FROM b.t;
SELECT 't_all after ATTACH PARTITION ALL', count() FROM b.t_all;

-- MOVE PARTITION TO TABLE (MergeTree -> MergeTree)
CREATE TABLE b.u_stage AS b.u;
INSERT INTO b.u_stage SELECT number + 10000, 'staged' FROM numbers(500);
CREATE TABLE b.u_dest AS b.u;
INSERT INTO b.u_dest SELECT number + 20000, 'mv-written' FROM numbers(7);
ALTER TABLE b.u_stage MOVE PARTITION tuple() TO TABLE b.u_dest;
SELECT 'after MOVE tuple()', (SELECT count() FROM b.u_stage) AS stage, (SELECT count() FROM b.u_dest) AS dest;
INSERT INTO b.u_stage SELECT number + 30000, 'staged2' FROM numbers(5);
ALTER TABLE b.u_stage MOVE PARTITION ALL TO TABLE b.u_dest;
ALTER TABLE b.u_stage MOVE PARTITION ID 'all' TO TABLE b.u_dest;
SELECT 'after MOVE ID all', (SELECT count() FROM b.u_stage) AS stage, (SELECT count() FROM b.u_dest) AS dest;
-- MOVE between different engine families
CREATE TABLE b.p_mt (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
CREATE TABLE b.p_rmt (id UInt64, ts DateTime, payload String) ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(ts) ORDER BY id;
INSERT INTO b.p_mt VALUES (1, '2026-01-05', 'a');
ALTER TABLE b.p_mt MOVE PARTITION 202601 TO TABLE b.p_rmt;
SELECT 'MT->RMT move', (SELECT count() FROM b.p_mt), (SELECT count() FROM b.p_rmt);
-- REPLACE PARTITION (idempotent overwrite)
CREATE TABLE b.p_rep AS b.p_mt;
INSERT INTO b.p_mt VALUES (2, '2026-01-06', 'b');
ALTER TABLE b.p_rep REPLACE PARTITION 202601 FROM b.p_mt;
ALTER TABLE b.p_rep REPLACE PARTITION 202601 FROM b.p_mt;
SELECT 'REPLACE twice', count() FROM b.p_rep;
-- MOVE PARTITION to a table with different ORDER BY
CREATE TABLE b.p_diff (id UInt64, ts DateTime, payload String) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY (payload, id);
ALTER TABLE b.p_mt MOVE PARTITION 202601 TO TABLE b.p_diff;
