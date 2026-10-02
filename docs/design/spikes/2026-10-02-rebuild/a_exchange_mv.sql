DROP DATABASE IF EXISTS a SYNC;
CREATE DATABASE a ENGINE = Atomic;
CREATE TABLE a.t     (id UInt64, src String) ENGINE = MergeTree ORDER BY id;
CREATE TABLE a.t_new (id UInt64, src String) ENGINE = MergeTree ORDER BY (src, id);
CREATE TABLE a.feeder (id UInt64, src String) ENGINE = MergeTree ORDER BY id;
CREATE TABLE a.sink_src (id UInt64, src String, via String) ENGINE = MergeTree ORDER BY id;
-- MV whose SOURCE is t
CREATE MATERIALIZED VIEW a.mv_src TO a.sink_src AS SELECT id, src, 'mv_src' AS via FROM a.t;
-- MV whose TARGET is t
CREATE MATERIALIZED VIEW a.mv_tgt TO a.t AS SELECT id, src FROM a.feeder;
-- the dual-write MV: source t, target t_new
CREATE MATERIALIZED VIEW a.t_dual TO a.t_new AS SELECT id, src FROM a.t;

SELECT name, uuid FROM system.tables WHERE database = 'a' AND name IN ('t','t_new') ORDER BY name;
SELECT name, dependencies_table, loading_dependencies_table, loading_dependent_table FROM system.tables WHERE database='a' ORDER BY name;

INSERT INTO a.t VALUES (1, 'pre-direct');
INSERT INTO a.feeder VALUES (2, 'pre-feeder');

EXCHANGE TABLES a.t AND a.t_new;

SELECT name, uuid FROM system.tables WHERE database = 'a' AND name IN ('t','t_new') ORDER BY name;
SELECT name, dependencies_table FROM system.tables WHERE database='a' ORDER BY name;
SHOW CREATE TABLE a.mv_src FORMAT TSVRaw;
SHOW CREATE TABLE a.mv_tgt FORMAT TSVRaw;
SHOW CREATE TABLE a.t_dual FORMAT TSVRaw;

INSERT INTO a.t VALUES (3, 'post-direct-into-name-t');
INSERT INTO a.t_new VALUES (4, 'post-direct-into-name-t_new');
INSERT INTO a.feeder VALUES (5, 'post-feeder');

SELECT * FROM (SELECT 'name t' AS tbl, id, src FROM a.t UNION ALL SELECT 'name t_new' AS tbl, id, src FROM a.t_new) ORDER BY tbl, id;
SELECT 'sink_src' AS tbl, id, src, via FROM a.sink_src ORDER BY id;
