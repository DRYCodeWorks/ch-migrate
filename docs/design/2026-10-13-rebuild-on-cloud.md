# Rebuilding tables on ClickHouse Cloud

**Date:** 2026-10-13  
**Status:** Cloud support chosen (Dan, 2026-10-13). The protocol below is proposed and awaits Dan's approval; nothing is implemented yet.

## Decision

ClickHouse Cloud supplies a consistent Shared catalog and immutable SharedMergeTree parts. It does **not** supply the three application-level fences used by the current `rebuild_table`: caller-chosen UUIDs, a frozen snapshot-helper part set, or an async-insert queue barrier at `EXCHANGE`. **Dan decided on 2026-10-13 that 1.0 fully supports Cloud, `rebuild_table` included**, as the [accepted 1.0 definition](2026-10-02-ch-migrate-1.0.md#8-oprebuild_table) already promises. The protocol below replaces each fence with one Cloud allows, keeps every fail-closed recovery rule, and lists what Dan is asked to approve.

**Until then:** `plan` reports and `up` refuses a rebuild on a Shared database with
the preflight finding `cloud_unsupported`, before any lock, helper or swap exists.
The integration test asserts the table, its rows and its catalog are unchanged.

## What the current engine protects

1. **Explicit UUIDs.** `_Lock._create` embeds the lock UUID, owner comment and isolated Keeper path; `_owned` refuses another owner's lock across hosts. `_initialize` records the old source UUID and three planned helper UUIDs *before* DDL; `helper_ddl` and `dual_ddl` embed UUIDs in CREATE. `_Context.ddl` persists intent before sending, refuses an unowned pre-existing result, and accepts a checkpointed result only on all-host UUID convergence. `_dual`, `_resolve_exchange`, `_cleanup`, `_result` and `_verify_complete` identify the owned view/helpers, prove the old source moved to the rollback name and the replacement took the live name, rather than trusting names alone. `_async_report` filters `FlushError` exceptions containing the **old UUID**. See `ch_migrate/rebuild_engine.py:_Lock._create,_Lock._owned,_initialize,_Context.ddl,_Context.uuid_map,_Context.converged,_dual,_exchange_evidence,_resolve_exchange,_cleanup,_async_report,_result,_verify_complete` and `ch_migrate/rebuild_ddl.py:helper_ddl,dual_ddl`. A `COMMENT` identifies the lock owner, but the UUID is also checked; a name or comment alone is not its current proof.
2. **`SYSTEM STOP MERGES` on `snap` and `stage`, not the live source.** `_helpers` stops both, `_snapshot` re-stops `snap` on restart, and `_copy` re-stops `stage`. `_snapshot` freezes per-partition row counts after attaching each source partition; `_copy_partition` uses those counts to distinguish an incomplete stage, a complete staged partition, and a moved partition without replaying an ambiguous MOVE. `_verify_stage` checks stage partition IDs and those counts on every replica. A `Replacing`/`Collapsing`/`Summing`/`Aggregating` merge on **snap** can change the physical row count or values between count capture and SELECT, making the stage-count equality impossible or, worse, making a count-only test insufficient to prove content. A merge on **stage** can change the count between verification and MOVE and invalidate the `{0, expected}` recovery states; even count-preserving merges change part names, but this code does **not** journal part names. Source merges are allowed; immutable attached source parts remain readable. See `ch_migrate/rebuild_engine.py:_helpers,_stop_merges,_snapshot,_copy,_copy_partition,_verify_stage` and [parts and merge model](https://clickhouse.com/docs/parts).
3. **`SYSTEM FLUSH ASYNC INSERT QUEUE`.** `_swap` flushes the old live name immediately before `EXCHANGE`, reducing but not closing the window for queued pre-swap writes. `_async_report` later flushes the **rollback name/old UUID**, drains pre-barrier processes, flushes logs, and bounds the old-UUID `FlushError` query by `swap_intent`/`report_end`. Without that second barrier, `async_flush_errors` could undercount errors not yet emitted, so `_result` cannot honestly report a final count, bytes or query IDs. Neither flush is atomic with concurrent enqueue. See `ch_migrate/rebuild_engine.py:_swap,_flush_async,_async_report,_drain_before,_result` and [D5 in the rebuild spike](2026-10-02-rebuild-spike.md#d-insert-races).

## Cloud guarantees and limits

- **Catalog identity: partial.** [Shared Catalog documentation](https://clickhouse.com/docs/cloud/reference/shared-catalog) describes centralized versioned database/table metadata and replica convergence. The [architecture account](https://clickhouse.com/blog/clickhouse-cloud-stateless-compute#stage-3-decoupling-metadata-with-a-shared-catalog) describes `/uuids` (object metadata), `/names` (name-to-UUID) and `/replicas` (applied version), with linearizable Keeper multi-writes for conflicting DDL. The [Atomic engine reference](https://clickhouse.com/docs/engines/database-engines/atomic) describes persistent UUIDs independent of names. Cloud assigns an object UUID and preserves it across `EXCHANGE` (probe below). It does **not** accept caller-assigned UUIDs in Shared databases: explicit table/view `UUID` returns Code 36, and `database_replicated_allow_explicit_uuid` is read-only; explicit replica name in SharedMergeTree arguments returns Code 80 ([Cloud spike](2026-10-02-rebuild-spike.md), DRY Cloud test service results, 2026-10-13). Catalog serializability is not an application lock or a durable, caller-owned CREATE receipt. `log_comment`/`query_id` in [query_log](https://clickhouse.com/docs/operations/system-tables/query_log) attribute a request, not a surviving object; log retention and per-node logging prevent treating absence as proof that CREATE did not run. A unique `COMMENT` plus issuer token in persisted intent can distinguish an owned result *after* catalog visibility, but a vanished object or an unacknowledged CREATE with no observable result remains unknown.
- **Parts and transfers: partial.** [SharedMergeTree](https://clickhouse.com/docs/cloud/reference/shared-merge-tree) shares storage and Keeper part metadata; replica metadata fetch is asynchronous, and `SYSTEM SYNC REPLICA … LIGHTWEIGHT` can fetch it. [MergeTree parts](https://clickhouse.com/docs/parts) are immutable; a merge publishes a new part rather than modifying the attached input. [ALTER PARTITION](https://clickhouse.com/docs/reference/statements/alter/partition#attach-partition-from) promises a copy with the source intact, and [MOVE PARTITION](https://clickhouse.com/docs/reference/statements/alter/partition#move-partition-to-table) removes from source. The DRY Cloud test service accepts both between same-schema SharedMergeTree tables and converges on two replicas (the prior A/B spike). Neither documentation nor that observation proves that every SharedMergeTree transfer is metadata-only or that cross-table MOVE is a single atomic transaction through a server crash. Do **not** sell the local hardlink timing from [the self-hosted spike](2026-10-02-rebuild-spike.md#b-attach-partition-from-and-move-partition) as a Cloud zero-copy guarantee. Immutable parts protect a snapshot already attached; they do not freeze `ReplacingMergeTree` semantics or a helper's active-part set. `SYSTEM SYNC REPLICA` synchronizes visibility, not merges or async-insert buffers.
- **Async DDL: no swap barrier.** [Async insert documentation](https://clickhouse.com/docs/concepts/features/operations/insert/asyncinserts) says buffers are per node and shape; flush rechecks destination catalog/authorization. `wait_for_async_insert=1` acknowledges after flush and returns errors, while `wait_for_async_insert=0` acknowledges before flush and has no delivery guarantee or dead-letter queue. It documents a server-wide `SYSTEM FLUSH ASYNC INSERT QUEUE` for maintenance, not an automatic queue flush on `EXCHANGE`/`DROP`. On the DRY Cloud test service, `SYSTEM FLUSH ASYNC INSERT QUEUE` (including the table-scoped form used by the engine) requires unavailable `SYSTEM FLUSH ASYNC INSERT QUEUE ON *.*` privilege (Code 497); Cloud does not supply that fence to this operator. A prior queued-swap probe observed Code 741 `TABLE_UUID_MISMATCH` and lost fire-and-forget rows. `insert_deduplication_token` is a [bounded retry/dedup aid](https://clickhouse.com/docs/concepts/features/operations/insert/deduplicating-inserts-on-retries), not a queue flush or an app-level migration lock. [asynchronous_inserts](https://clickhouse.com/docs/operations/system-tables/asynchronous_inserts), [asynchronous_insert_log](https://clickhouse.com/docs/operations/system-tables/asynchronous_insert_log), `query_log` and `system.processes` are per node; inspect all replicas, with `skip_unavailable_shards=0` when completeness is required. Their existence does not prove absence of writes from another compute service.

## Synthetic Cloud observations

The DRY Cloud test service ran ClickHouse 26.6.1.2326 with two replicas and a Shared database. The following are **observations**, not general Cloud contracts; all database names are replaced by `it_design_db` and UUIDs by `U_old`/`U_new`. Client credentials are not recorded.

| SQL / sequence | Observed result |
|---|---|
| `SELECT name,value FROM system.settings WHERE name IN ('async_insert','wait_for_async_insert','async_insert_busy_timeout_ms','async_insert_use_adaptive_busy_timeout')` | For `chm_it`: `1`, `1`, `1000`, `1`, respectively. This is the service/user effective default, not a guarantee for other writer profiles. |
| `CREATE TABLE it_design_db.t (id UInt64,p UInt8) ENGINE=MergeTree PARTITION BY p ORDER BY id COMMENT 'design-owner-token'` with setting `log_comment='design-ddl-token'`; query `clusterAllReplicas('default',system.tables)` | Both replicas returned the same server-assigned `U_old` and comment. After `EXCHANGE TABLES it_design_db.t AND it_design_db.next`, both showed `U_old` and its comment under `next`, and `U_new` under `t`. |
| `SYSTEM FLUSH LOGS`; query `clusterAllReplicas('default',system.query_log)` for `log_comment='design-ddl-token'` | `QueryStart`/`QueryFinish` entries with the CREATE text and token were present (two node-local query IDs observed). This proves positive request attribution in this run, not an always-available durable receipt. |
| `SYSTEM SYNC REPLICA it_design_db.t LIGHTWEIGHT` | Succeeded for `chm_it`; SharedMergeTree documentation permits it for read-after-remote-write metadata visibility. |
| Three inserts into partitions 1 and 2; `ALTER TABLE it_design_db.snap ATTACH PARTITION ID '1' FROM it_design_db.t`; `OPTIMIZE TABLE it_design_db.t PARTITION ID '2' FINAL`; `ALTER TABLE it_design_db.snap ATTACH PARTITION ID '2' FROM it_design_db.t` | Both ATTACH steps succeeded and both replicas read `snap` as eight rows, sum(id)=241 (six source rows plus two independently inserted snap rows). The `OPTIMIZE` command succeeded, but this probe did not prove its source merge completed before the second ATTACH. Its immediate `system.parts` aggregation failed; the separate helper-merge probe below does prove a changed part set. |
| `CREATE TABLE it_design_db.source/snap (…) ENGINE=ReplacingMergeTree(v) PARTITION BY p ORDER BY id`; two inserts with `(id,v,p)=(1,1,1),(1,2,1)`; `ALTER TABLE snap ATTACH PARTITION ID '1' FROM source`; `OPTIMIZE TABLE snap PARTITION ID '1' FINAL`; `SYSTEM SYNC REPLICA snap LIGHTWEIGHT` | Before the optimize, both tables showed two active parts and two physical rows in partition `1`. After convergence, the source retained parts `1_0_0_1`,`1_1_1_1` and count 2, while `snap` had part `1_0_1_2` and count 1 (sum(v)=2); replica-wide `system.parts` reported the same merged part on both replicas. The helper merge changed row count, value set and part names **without** changing the source. |
| `CREATE TABLE … a0/b0` and `a1/b1` (same schema), `INSERT INTO a0/a1 VALUES (…)` with `async_insert=1, wait_for_async_insert=0/1, async_insert_use_adaptive_busy_timeout=0, async_insert_busy_timeout_ms=15000`; observe `clusterAllReplicas('default',system.asynchronous_inserts)` nonempty, then `EXCHANGE TABLES aN AND bN` | For `wait=0`, queue held five bytes on one node and client had already received `ACK`; both tables subsequently had zero rows. For `wait=1`, queue held five bytes, client had **not** received ACK at swap, then received Code 741; both tables had zero rows. The logged wait=1 entry was `FlushError` with `TABLE_UUID_MISMATCH`. A fire-and-forget `FlushError` log entry was not observed in this sample: never equate missing logs with absence of loss. |
| Earlier A/B/D4/D5 Cloud spike: `EXCHANGE`, `ATTACH PARTITION FROM`, `MOVE PARTITION TO TABLE`, in-flight insert across swap then `DROP TABLE dual SYNC` | Same name-following MV, accepted transfers and two-replica visibility as self-hosted; in-flight insert landed fully and DROP waited about 13 seconds. The end-to-end rebuild stopped at the denied queue flush. These observations do not prove arbitrary concurrent-load or crash recovery. |
| Prior Cloud spike: `CREATE TABLE … UUID '…'` / `CREATE MATERIALIZED VIEW … UUID '…'`; `SET database_replicated_allow_explicit_uuid=1` (or `2`); `CREATE TABLE … ENGINE=ReplicatedMergeTree('path','replica')`; `SYSTEM STOP MERGES it_design_db.snap`; `SYSTEM FLUSH ASYNC INSERT QUEUE it_design_db.t` | Explicit UUID rejected (Code 36) in Shared; setting is read-only; explicit replica name rejected (Code 80); STOP MERGES access denied and not grantable; table-scoped queue flush access denied (Code 497, requires grant `ON *.*`). These are results on this service, not proof about future Cloud releases. |

### Protocol probes (2026-10-13, same service)

| Probe | Result |
|---|---|
| Replacing, Collapsing, Aggregating and Summing source (with TTL) → `ATTACH PARTITION ID '1' FROM` into a **plain** `MergeTree` snap with the same columns, ORDER BY and PARTITION BY and no TTL; `OPTIMIZE ... FINAL` on snap; `INSERT ... SELECT` into a plain stage; `OPTIMIZE ... FINAL`; `MOVE PARTITION` stage → a target with the source's engine | Every step accepted for all four families. The plain helpers kept all 3 physical rows through `OPTIMIZE FINAL` on both replicas, and the MOVE delivered 3 rows to the family target and emptied the stage. |
| `CREATE TABLE ... COMMENT '<token>'`, then `EXCHANGE` | Both replicas show one server UUID with the token. After `EXCHANGE`, UUID and comment travel together under the other name. `ALTER TABLE ... MODIFY COMMENT` is allowed, so an edited comment must count as lost ownership. |
| `CREATE MATERIALIZED VIEW v TO dst COMMENT '<token>' AS SELECT ...` | Accepted; the token shows in `system.tables.comment` on both replicas and the view delivers rows. `... AS SELECT ... FROM src COMMENT '...'` fails (62) because `COMMENT` parses as a table alias, so put `COMMENT` before `AS`. |
| `SYSTEM SYNC REPLICA ON CLUSTER 'default' t LIGHTWEIGHT`, `KILL QUERY ON CLUSTER 'default' ... SYNC`, `SYSTEM FLUSH LOGS ON CLUSTER 'default'`; `system.clusters` | All accepted for `chm_it`. Cluster `default` has 1 shard and 2 replicas. |
| Six concurrent async inserts (three `wait=0`, three `wait=1`, busy timeout 30 s) queued across both replicas, then `EXCHANGE` with **no** queue flush; three trials | The old table's queue entries (`system.asynchronous_inserts`, cluster-wide) were gone 0.15–0.23 s after `EXCHANGE`, long before the busy timeout. All `wait=0` inserts were acknowledged and lost; all `wait=1` inserts got 741. After `SYSTEM FLUSH LOGS ON CLUSTER`, `asynchronous_insert_log` held old-UUID `FlushError` rows for 6, 5 and 6 of the 6 queued inserts. |
| Self-hosted 26.3: `SYSTEM STOP MERGES` on a ReplacingMergeTree, two inserts, restart the server, `OPTIMIZE FINAL` | Before restart the merge was refused (236) and 2 physical rows remained. **After restart the merge ran and 1 row remained: `STOP MERGES` does not survive a restart.** |

## Proposed Cloud protocol (for approval)

### 1. Ownership: owner-token comment, then read back the UUID

On a Shared database, `_initialize` no longer generates helper UUIDs. It journals a token for each role (`chm-rebuild:<revision>:<role>:<uuid4>`) before any CREATE. The lock, `new`, `snap`, `stage` and the dual view are created **without** `UUID` and **with** `COMMENT '<token>'`, using plain engine names that Cloud converts to Shared, with no Keeper path. For the view, `COMMENT` goes before `AS`.

An object is ours only when every replica (`clusterAllReplicas('default', system.tables)`) shows the name with exactly our token and one common UUID. That UUID is then journaled ("bound"). From that point every existing UUID check runs unchanged against the bound value: exchange evidence, rollback identity, cleanup, old-UUID async attribution and the result.

| Resume finds | Action | Rule |
|---|---|---|
| Intent journaled, name absent, CREATE not acknowledged | `UnknownOutcome`; operator reconciles | Same as today for unacknowledged local DDL |
| Name with our token on every replica, UUID not yet bound | Bind and continue | The catalog proves ownership; covers a crash between acknowledgement and bind |
| Name with another comment, or replicas disagree on UUID | `UnknownOutcome`; never adopt | Same as the unowned pre-existing rule |
| Bound object whose comment changed later | Ownership lost; fail closed | New case; there is no takeover path |

The lock keeps its heartbeat and the 2026-10-04 rule: expiry never authorizes takeover; the operator confirms and releases. Self-hosted keeps chosen UUIDs, because its helper Keeper paths embed them.

### 2. Snapshot without `STOP MERGES`: plain, TTL-free helpers

`snap` and `stage` become plain `MergeTree`, in the same replication family as the source (Shared on Cloud; `ReplicatedMergeTree` with a distinct Keeper path on replicated self-hosted). They keep the same columns, ORDER BY, PARTITION BY, PRIMARY KEY, indexes and projections, but drop engine parameters and every table and column TTL. Plain merges keep every physical row, and with no TTL nothing expires. The per-partition counts taken at the snapshot therefore stay exact however often the helpers merge, and the `{0, expected}` MOVE recovery states stay valid. `new` keeps the target engine. Preflight compares source → snap and stage → target for transfer compatibility (columns, ORDER BY, partition key) instead of requiring equal engines for the helpers.

**Proposed for self-hosted as well, deleting `STOP MERGES`.** The restart probe shows `STOP MERGES` silently lapses when a node restarts. After such a restart, a Replacing, Collapsing, Summing or Aggregating snapshot can merge away rows, so `_verify_stage` never sees the count it expects. [INFERENCE] The run would then fail closed and need an operator, rather than lose rows. One mechanism that holds by construction is simpler than two. Cost: DRY-1410's continuous-writer acceptance (5 variants × 5 runs) must be re-run on the new helpers.

### 3. Swap without a forced queue flush

On Cloud the pre-`EXCHANGE` flush is skipped. `EXCHANGE` itself drained the old queue within about 0.2 s in every trial. The guarantee is unchanged: synchronous and `wait=1` rows are never lost (`wait=1` writers get 741 and retry), and `wait=0` writers still need `allow_unacknowledged_async_loss=True`.

After the swap, the flush of the rollback table is replaced by a cluster-wide wait: no `system.asynchronous_inserts` entry for the table with `first_update` at or before the `EXCHANGE` acknowledgement may remain on any replica. Then come the existing drain of in-flight flushes, `SYSTEM FLUSH LOGS ON CLUSTER 'default'`, and the old-UUID `FlushError` query, repeated until two consecutive reads agree. On Cloud the report says `at least N` and names the replicas it observed, because one trial logged 5 of 6. Self-hosted keeps the flush.

### 4. Topology and other writers

For a Shared database the engine inspects through cluster `default`: hosts, catalog, processes, counts, logs, `KILL QUERY` and `SYSTEM SYNC REPLICA ... LIGHTWEIGHT`, all accepted in the probes. DDL never uses `ON CLUSTER`. Source and `new` are synchronized on Shared as they are on Replicated today.

**Gap: writers on other compute services.** Under Cloud compute-compute separation, another service's replicas, queues and running inserts are invisible to this one. An insert from such a service that started before the dual view reached it would skip the view. If it commits after the snapshot, an acknowledged row would be missing from the new table. That case breaks the guarantee, and ch-migrate cannot detect it from inside the service. Proposal: on Cloud the migration must declare `writers_on_this_service_only=True`. Like the async-loss opt-in, there is no command-line bypass. It comes with a configurable minimum grace period after the dual view is visible, and `plan` states that the condition cannot be checked.

### Acceptance for this protocol

- **Unit:**
  - every row of the ownership table;
  - plain, TTL-free helper DDL for each engine family;
  - the queue-drain and report logic.
- **Docker, single node and cluster:**
  - all rebuild suites;
  - DRY-1410's five continuous-writer variants, five consecutive runs;
  - a new node-restart-during-copy test.
- **Cloud:**
  - scaffolded rebuild round trip;
  - the rebuild command and dependent suites;
  - continuous-writer variants `sync`, `async1` and `async0` against the service;
  - SIGKILL mid-copy, then operator release and resume;
  - rebuild spike E re-run to completion and recorded in rebuild-spike section C.
- The `cloud_unsupported` refusal is removed only when all of the above pass.

### What approval covers

1. Token-comment ownership with read-back UUIDs on Shared databases (self-hosted unchanged).
2. Plain, TTL-free `snap`/`stage` helpers everywhere, deleting `STOP MERGES`, plus the DRY-1410 re-run.
3. A Cloud swap without forced flushes, with the async report as an observed lower bound.
4. The required `writers_on_this_service_only=True` declaration plus grace period on Cloud, or another way to handle other-service writers.
