# Spike: rebuild_table mechanics (open questions 10–11)

**Date:** 2026-10-02 · **For:** capability 8 of `2026-10-02-ch-migrate-1.0.md` · **Ticket:** DRY-1406
**Setup:** ClickHouse 26.3.32.14 in a throwaway local container, with embedded Keeper for
replicated runs. Every experiment's SQL and output is in `spikes/2026-10-02-rebuild/`. Files are
named after the sections below: `a_*`, `b*_*`, `d*_*`, `f*_*`, and `e2e.py` with `run_e2e*.sh`.

## Summary

The planned rebuild works with writers running. Across every end-to-end run, all 6 million
preloaded rows were present exactly once after the swap, and every duplicate fell inside the
expected window.

Four things change from the planned mechanism:

1. **The swap order** (A).
2. **The guarantee must be narrowed** for fire-and-forget async inserts (D5).
3. **Resuming needs a kill step and a per-table lock.**
4. **1.0 should not change the partition key** (F4).

**Materialized views follow the table name across `EXCHANGE`,** so dependent views need no
re-pointing.

**Decision (Dan, 2026-10-02):** all four are adopted. The guarantee is now "no acknowledged row is
lost". Fire-and-forget async writers need an opt-in written in the migration file. Partition-key
changes are refused in 1.0. Cloud is verified on a DRY-owned Cloud service at each acceptance
check (DRY-1413). Implementation: DRY-1407.

## End-to-end runs

Writers inserted throughout. The sorting key changed from `ORDER BY id` to `(k, ts, id)`. The sync
writer used 2,000-row batches, and the async writers used 200-row batches.

| Run | Writer ids missing (sync / async wait=1 / async wait=0) | Duplicates inside / outside the window | Downstream view got each id once |
|---|---|---|---|
| MergeTree | 0 / 0 / **200** | 88,400 / 0 | yes |
| MergeTree, killed mid-copy, then resumed | 0 / 0 / 0 | 50,756 / 200 (wait=0 rows flushed just after step 1; see D2) | yes |
| ReplicatedMergeTree in a Replicated database | 0 / 0 / **400** | 68,051 / 0 | yes |

The missing rows are fire-and-forget async inserts (`wait_for_async_insert=0`) still buffered at
the moment of the swap; see D5.

## A. Do materialized views follow the name or the table?

**They follow the name, in both directions.** Verified in an Atomic database and in a Replicated
database (`a_exchange_mv`, `a3_replicated_db`).

- A view reading from `t` fires, after `EXCHANGE`, for inserts into whatever is now named `t`.
  `system.tables.dependencies_table` is unchanged by the swap.
- A view with `TO t` writes into the new `t` after the swap.
- Just after the swap, `t_dual` reads from the new `t` and writes into the old table (now named
  `t_new`). That is harmless and not a loop.

**Step 5 order:**
1. `SYSTEM FLUSH ASYNC INSERT QUEUE db.t`. This narrows the D5 gap; it does not close it.
2. `EXCHANGE TABLES db.t AND db.t_new`.
3. `DROP TABLE db.t_dual SYNC`. This is a barrier: it waited 10.9 s for an insert that started
   before the swap, and all of that insert's rows reached the new table (D4).
4. Check by UUID that the old table is the one now named `t_new`. Then rename it (for example
   `t_old_<rev>`, kept as a rollback path for a grace period) or drop it. Drop `t_snap` and
   `t_stage`.

Never drop `t_dual` before `EXCHANGE`: rows inserted in between would be lost.

## B. ATTACH PARTITION FROM and MOVE PARTITION

**Cost:** hardlinks only; source and copy share inodes. Each partition of a 5M-row table took
0–2 ms on MergeTree and 14–20 ms on ReplicatedMergeTree.

**Replicated:** works. The second replica applied a `REPLACE_RANGE` log entry, and both ended with
5,000,000 rows.

**What must match between the tables:**
- **Rejected:** a different ORDER BY, partition key or column type, or an extra column.
- **Accepted:** a different column order, codec, `index_granularity` or TTL; extra skip indexes or
  projections on the destination (the attached parts lack them until `MATERIALIZE`).
- **MergeTree → ReplacingMergeTree is accepted silently,** so ch-migrate must check engines
  itself.
- **MOVE PARTITION TO TABLE** also needs the same engine family; MergeTree → ReplicatedMergeTree
  fails with error 48.

**Unpartitioned tables:**
- ATTACH works with `tuple()`, `ID 'all'` and `ALL`.
- MOVE works with `tuple()` and `ID 'all'`, but `MOVE PARTITION ALL` is rejected (344).
- So use `PARTITION ID '<id>'` everywhere.

**Gotcha:** `CREATE TABLE t_snap AS t` fails with `REPLICA_ALREADY_EXISTS` when `t` has an explicit
Keeper path. Generate DDL with a distinct path for each helper table.

## C. ClickHouse Cloud: unverified

- No ClickHouse doc says whether SharedMergeTree supports `ATTACH PARTITION FROM` or `MOVE
  PARTITION TO TABLE`.
  - The [ALTER PARTITION page](https://clickhouse.com/docs/sql-reference/statements/alter/partition)
    lists its requirements with no Cloud exception.
  - The [SharedMergeTree page](https://clickhouse.com/docs/cloud/reference/shared-merge-tree) says
    replicas fetch metadata changes asynchronously.
  - The [Cloud compatibility page](https://clickhouse.com/docs/whats-new/cloud-compatibility) says
    nothing.
- `EXCHANGE` is documented as supported on the Atomic and Shared database engines, and Shared is
  Cloud's ([EXCHANGE](https://clickhouse.com/docs/reference/statements/exchange)). The same
  sentence excludes the Replicated engine, yet 26.3 ran it there without error. Test each
  self-hosted version we support.
- Swapping several pairs in one `EXCHANGE` is sequential, not atomic. Swap one table per
  statement.
- ClickHouse#116383 (open): in a Replicated database, a `MOVE PARTITION … TO TABLE` whose target
  is in another database wedges the DDL queue. **Keep every helper table in `t`'s database.**
- Cloud risks reasoned, not tested:
  - With compute-compute separation, inserts through another service don't appear in this
    service's `system.processes`. Step 2 needs a configurable minimum grace period there, and
    `plan` should say it is best effort.
  - Replicas load metadata asynchronously. Wait until `t_dual` is visible on every replica
    before taking step 2's timestamp.
- **Before promising Cloud:** run the A, B, D4, D5 and E scripts against a Cloud service. Only the
  host changes.

## D. Insert races

**D1. An INSERT that started before `CREATE MATERIALIZED VIEW` does not go through the view.**
- The view chain is fixed when the insert pipeline is built.
- A slow insert of 20,000 rows, with the view created after 4,000, left 0 rows in `t_new`.
- So step 2, waiting for inserts already running, is required.

**D2. Async inserts buffered before the view but flushed after it do go through it.** The flush
builds its own pipeline.
- So for async inserts, the duplicate window is set by **server flush time**, not client send
  time.
- The window is therefore defined as: rows committed or flushed between `t_dual`'s creation and
  the snapshot's completion, in server time.
- In-progress flushes appear in `system.processes` as `query_kind = 'AsyncInsertFlush'`.

**D3. Detecting in-flight inserts for step 2.** `system.processes` in 26.3 has no `tables` and no
`query_start_time` column. The prototype polled until this returned nothing:

```sql
SELECT query_id FROM system.processes
WHERE query_kind IN ('Insert', 'AsyncInsertFlush')
  AND now64(6) - toIntervalMillisecond(toUInt64(elapsed * 1000)) <= <view creation time>
```

- Use `clusterAllReplicas` on clusters.
- Record a server `now64(6)` right after the CREATE, rather than the second-precision metadata
  time.
- The query is conservative: it waits for every early insert on any table. Add a timeout, and
  optionally narrow it with `system.query_log` QueryStart `tables`, which lists the whole view
  cascade.
- In the runs it waited 0.8–2.1 s.

**D4. A synchronous insert in flight across EXCHANGE: safe.**
- `EXCHANGE` took 0 ms.
- `DROP TABLE t_dual SYNC` waited 10.9 s for the in-flight insert, and all 15,000 of its rows
  reached the new table through the captured `t_dual`.

**D5. Async-insert entries queued against `t` before EXCHANGE are rejected when flushed after it.
This is the gap.**

- **Mechanism:** a queue entry is keyed by the table's name and UUID at enqueue time. At flush,
  `t` resolves to the new UUID and the server refuses with `TABLE_UUID_MISMATCH` (741). It does
  not write to the old table either.
- **`wait_for_async_insert=1` writers** (the 26.3 default) get error 741 and retry. They lost
  nothing.
- **`wait_for_async_insert=0` writers** were already told OK, so their rows are gone. Only
  `system.asynchronous_insert_log` records it, as `FlushError`.
- Flushing the queue just before the swap does not prevent it. An entry arrived during the flush
  and failed 200 ms later.
- **Nothing closes the gap:**
  - Flush, re-check, then swap is not atomic.
  - Checking afterwards only detects; the rejected data isn't stored anywhere.
  - There is no per-table switch to block async inserts. The only blunt option is temporarily
    revoking INSERT around the swap, which is untested and not recommended for 1.0.
- **Recommendation for 1.0:**
  - **Guarantee:** no *acknowledged* row is lost. Rows written with `wait_for_async_insert=0` that
    are still buffered at the instant of the swap may be dropped, as ClickHouse's own
    fire-and-forget mode allows.
  - **Preflight:** check `query_log` and the writers' profiles for `wait_for_async_insert=0`
    writers to the table, and require an explicit opt-in in the migration file.
  - **After the swap:** report the `FlushError` count and query_ids.
  - **Docs:** writers should retry error 741.

## F. What can break the plan

1. **`t_dual` adds ways for writers' inserts to fail.** Every insert into `t` also writes into
   `t_new`, and a failure there fails the writer's INSERT. Causes:
   - too many parts in `t_new`;
   - `max_partitions_per_insert_block`, if the new partition key is finer;
   - CHECK constraints;
   - memory.

   Never set `materialized_views_ignore_errors = 1`: it would silently lose rows. Part and merge
   load roughly double during the rebuild. `plan` should show part counts per partition and the
   insert rate.
2. **A failed insert, then a retry** (`f1_mv_failure_retry`).
   - A row that fails only in `t_new` lands in the old `t` alone. If it is never retried, it is
     lost after the swap, but it was never acknowledged.
   - With ReplicatedMergeTree defaults, a retry is deduplicated in `t` and still fed to `t_new`,
     which is correct.
   - *Not tested:* with `deduplicate_blocks_in_dependent_materialized_views = 0`, a deduplicated
     retry would skip the view. `plan` should warn when it is 0 on replicated tables.
3. **Mutations and partition DDL on `t` after the snapshot are not carried over.** The view copies
   only inserts. Refuse to start while mutations on `t` are unfinished, and document that the
   table takes no mutations or partition DDL during a rebuild.
4. **Changing the partition key breaks clean resume.** ATTACH and MOVE require identical partition
   keys, so with a new key one source partition fans out into several target partitions. A crash
   between them would leave a partition half-moved, and redoing it would duplicate rows.
   **1.0 keeps the partition key and refuses otherwise.**
5. **Memory** scales with insert block size (about 256 MiB per block per thread in 26.3), not with
   partition size. Use a low `max_insert_threads` and set `max_memory_usage` on the copy.
6. **Disk:** the snapshot pins the old parts while `t` keeps merging, and `t_new` is a full copy.
   Peak is roughly 2–3× the table. `plan` should check free space.
7. **Killed mid-copy:** the server-side `INSERT … SELECT` keeps running after the client dies.
   - Partial rows land only in `t_stage`, so always stage and then MOVE. Never `INSERT … SELECT`
     straight into `t_new`.
   - *Not tested:* whether a non-replicated `MOVE PARTITION` is atomic across a server crash.
8. **Sharded or Distributed tables** (reasoned): the swap can't be atomic across shards. Refusing
   them in 1.0 is sound.
9. **Replicated tables in a plain database on a multi-host cluster** (reasoned): run
   `EXCHANGE … ON CLUSTER`, wait for every host, then `DROP TABLE t_dual ON CLUSTER … SYNC`. Drop
   `t_dual` only after every replica has swapped.

## Resuming after a kill, and the per-table lock

**Kill on resume (verified).** The rebuild was SIGKILLed while copying a partition, and the orphan
copy kept writing on the server. A naive re-run would have started a second copy beside it and
duplicated rows. The procedure, for each partition not yet marked moved:

1. Every stage copy runs with a deterministic query_id:
   `chm-rebuild-<revision>-<db>.<table>-<partition_id>`.
2. Recorded "staged" and `t_stage` empty: the MOVE committed but the record didn't. Mark the
   partition moved.
3. Recorded "staged" and the `t_stage` count equals the snapshot count: go straight to the MOVE.
4. Otherwise:
   - `KILL QUERY WHERE query_id = '<id>' SYNC` (on a cluster, on every node);
   - `ALTER TABLE t_stage DROP PARTITION ID '<p>'`;
   - copy again under the same query_id, check the count, and record "staged".
5. `ALTER TABLE t_stage MOVE PARTITION ID '<p>' TO TABLE t_new`, then record "moved".

In the run, the resume cancelled the orphan, redid two partitions, and finished with 0 missing ids
and 0 duplicated preload rows.

**A per-table lock is required.** The first prototype run killed only the `uv` wrapper. Two
rebuilds of the same table then ran at once and left the tables in a bad state.

Proposal (untested):
- `CREATE TABLE <db>._chm_rebuild_lock_<t> (owner String, heartbeat DateTime)` acts as the mutex,
  because CREATE fails if the table already exists.
- The holder updates its heartbeat.
- A newcomer takes over only after the heartbeat expires, and only after running the KILL step
  for the stale owner's query_ids.
- The lock is released with DROP.

## Other changes to the mechanism

- **Step 1** first checks for a finished or half-finished rebuild. Record the old and new UUIDs;
  if `t` already holds the new UUID, only step 5 cleanup remains.
- **Step 2:** take the timestamp only after `t_dual` is visible on every replica, include
  `AsyncInsertFlush`, use `clusterAllReplicas`, and add a timeout.
- **Helper DDL:** generated, not `CREATE … AS`. Keep every helper in `t`'s database with distinct
  Keeper paths. `t_snap` must match `t` exactly, and `t_stage` must match `t_new` exactly.
- **Preflight refusals:** pending mutations on `t`; a sharded or Distributed table; a target
  engine that ATTACH would accept silently but that differs from the source; a partition-key
  change.
- **Preflight warnings:** `deduplicate_blocks_in_dependent_materialized_views = 0`; free disk;
  part counts.
- **Writer guidance:** expect transient errors during a rebuild (741 at the swap, and failures
  writing into `t_new`), and retry.

## Not verified

- ClickHouse Cloud and SharedMergeTree, all of it.
- Real multi-host replicas: whether a replica clones parts locally or fetches them, and ON CLUSTER
  timing.
- Whether a non-replicated `MOVE PARTITION` is atomic across a server crash.
- Inner-engine views without `TO`, dictionaries, and Distributed tables across `EXCHANGE`.
- Versions before 26.3: the async-insert defaults, the default of
  `deduplicate_blocks_in_dependent_materialized_views`, and `EXCHANGE` in a Replicated database.
- The REVOKE-based block on async inserts, and the CREATE TABLE lock.
- Scale beyond 6 million rows.
