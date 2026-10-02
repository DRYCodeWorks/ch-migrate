# Spike: the official clickhouse-connect dialect (open questions 1–4)

**Date:** 2026-10-02 · **For:** capability 3 of `2026-10-02-ch-migrate-1.0.md` · **Ticket:** DRY-1391
**Setup:** ClickHouse 26.3.32.14 in throwaway local containers (one with Keeper). Legacy runs used
our real `env.py`. Scripts, migrations and logs are in `spikes/2026-10-02-dialect/`.

## Summary

- The official dialect (clickhouse-connect 1.9.0 with `[alembic]`) keeps a `SET` in effect across
  statements and revisions. Our current stack (clickhouse-sqlalchemy 0.3.2) loses it.
- The official dialect cannot run on Python 3.9. Every release that ships the Alembic integration
  requires Python 3.10 or later and refuses 3.9 at import. Alembic itself has required 3.10 since
  1.17.0.
- The fallback, clickhouse-sqlalchemy with an explicit session id passed through `connect_args`,
  was also verified to keep a `SET` in effect.
- **Independent of the dialect, today's version table can re-run a migration** (Q2). This must be
  fixed in 0.6.
- **Decision (Dan, 2026-10-02):** 0.6 adopts the official dialect and requires Python 3.10 or
  later. This replaces the product definition's original rule, which made 3.9 a hard requirement
  and would have forced the fallback. 0.5.x is the last line for 3.9. Implementation: DRY-1392
  (dialect) and DRY-1412 (version table).

## Q1. Does a `SET` carry over to the next statement?

**Verdict:** works on the official dialect. Today's setup loses it. A URL `session_id` doesn't
help, but `connect_args` does.

The test migration ran `SET max_threads = 1` through `op.execute`, then read the setting back in
the same revision and in the next one. The server default is 16.

| Setup | Same revision | Next revision |
|---|---|---|
| Official 1.9.0, default URL | 1 | 1 |
| clickhouse-sqlalchemy as shipped | 16 | 16 |
| clickhouse-sqlalchemy + URL `?session_id=…` | 16 | 16 (ignored silently, exit 0) |
| clickhouse-sqlalchemy + `connect_args={"ch_settings": {"session_id": …}}` | 1 | 1 |

- **Server-side proof:** in `system.query_log`, the official run shows `max_threads = 1` on every
  statement after the `SET`. The legacy runs never show it.
- **Why the official dialect works:** each SQLAlchemy connection gets its own client with a
  generated session id (`driver/httpclient.py:185-193`), on by default (`common.py:88`).
- **Why the URL parameter fails:** clickhouse-sqlalchemy copies URL parameters into transport
  options (`drivers/http/base.py:38`). Only the `ch_settings` dict is sent to the server
  (`transport.py:93-123, 166-168`), and a URL can't carry it.
- **Catch: idle sessions expire silently.** After the session timeout (default 60 s), ClickHouse
  quietly starts a fresh session under the same id, and every `SET` is gone.
  - Reproduced with curl: with `session_timeout=2` and 4 s idle, `max_threads` went 1 → 16.
  - With `session_check=1`, the same sequence fails loudly (`Code: 372 SESSION_NOT_FOUND`).
  - On the official dialect, `?session_timeout=` in the URL reaches the server (verified). It
    should also travel in clickhouse-sqlalchemy's `ch_settings`, but that is not run.
  - This matters for 0.7, where `up` may sit idle on the migration connection while it polls.

## Q2. The version table

**Verdict:** the official design is safe if interrupted. Ours can re-run migrations. Neither is
correct out of the box on self-hosted replicated clusters.

**What each creates and how the version advances:**

| | Official | Ours (0.4.1 `env.py`) |
|---|---|---|
| Table | `MergeTree ORDER BY version_num` (`impl.py:147-161`) | `ReplacingMergeTree ORDER BY updated` |
| Advancing the version | `INSERT` the new version, then `ALTER TABLE … DELETE WHERE version_num='old' SETTINGS mutations_sync = 2` (`impl.py:361-373`) | `ALTER TABLE … UPDATE version_num='new' WHERE …`, which returns before the update is applied |
| Downgrade | the same DELETE, waiting for it to finish | a DELETE that doesn't wait |

**A delayed update, simulated** with `SYSTEM STOP MERGES` on `alembic_version`:

- **Ours:** `up` ran r002 and r003 and exited 0, but the table still said `r001`. A second `up`
  re-ran r002 and r003, and the probe table shows r002 ran twice. **This is a bug in shipped
  code.**
- **Official:** `up` blocked on the DELETE. Killed mid-wait, the table held `r001` and `r002`. The
  next `up` refused ("r002 overlaps with other requested revisions r001") instead of re-running,
  and the table healed to `r002` once the DELETE finished.

**Existing 0.x projects.** The official impl drove our existing `ReplacingMergeTree` version table
correctly: downgrade, upgrade, downgrade to base and upgrade again, with `FINAL` and non-`FINAL`
reads agreeing. Switching dialect doesn't require migrating the table.

**Self-hosted replicated** (one replica with Keeper; the multi-replica effect is reasoned):
- In a `Replicated` database, both setups create the version table as a local, non-replicated
  table. Neither appears in `system.replicas`. On 26.3,
  `database_replicated_allow_only_replicated_engine = 0` allows this.
- So each node keeps its own migration state.
- In an ordinary Atomic database neither setup creates the table `ON CLUSTER`, so it exists only
  on whichever node you connected to.
- Successive `up` runs routed to different nodes would re-run migrations.

**ClickHouse Cloud** (docs only, not tested):
- MergeTree-family tables become SharedMergeTree on Cloud, so the version table is shared.
- The official `mutations_sync = 2` should therefore be correct there.
- Ours has the same re-run window as above, widened by replica lag.

**Changes for 0.6 (they apply on either dialect):**

1. **Advance the version as INSERT then a waited DELETE.** In the impl class's `_exec`, rewrite
   Alembic's version-table Update and Delete, as the official impl does (`impl.py:170-173,
   390-404`).
   - An interrupted run then leaves two rows, and Alembic refuses to continue: it fails closed.
   - *Not tested:* simply adding `mutations_sync = 2` to the existing UPDATE would still re-run
     a migration if killed mid-wait, because the UPDATE is still pending.
2. **New tables use `ORDER BY version_num`.** *Not tested:* with `ORDER BY updated` (one-second
   resolution), two heads written in the same second can collapse into one.
3. **Choose the engine from the deployment:**
   - Cloud: plain `MergeTree`.
   - Self-hosted `Replicated` database: `ReplicatedMergeTree` with no arguments (verified through
     a subclassed impl; it appears in `system.replicas`).
   - Atomic database with `cluster:` configured: `ReplicatedMergeTree(...) ON CLUSTER` with an
     explicit Keeper path.
   - Detect the case with `system.databases.engine` plus the `cluster` config.
4. **One unique session per run** (`uuid4`), and a `session_timeout` above the default for long
   runs.

## Q3. Bootstrap

**Verdict:** independent of the dialect. `bootstrap.py` uses the clickhouse-connect client
directly (`bootstrap.py:329-363`) and never imports SQLAlchemy.

- Ran with clickhouse-connect 1.9.0: completed.
- Migrations then ran as the bootstrapped restricted user, with a password needing URL encoding,
  and succeeded.
- Both packages install side by side:
  - Importing the official dialect claims `clickhouse://` and `clickhousedb://`.
  - `clickhouse+http://` stays with clickhouse-sqlalchemy.
- clickhouse-sqlalchemy 0.3.2 (the latest) pins `sqlalchemy>=2.0,<2.1`, so it is falling behind
  SQLAlchemy.

## Q4. Python 3.9

**Verdict:** not supported.

- **`requires-python` on PyPI:**
  - clickhouse-connect 0.9.2–0.15.1: `>=3.9`.
  - From 1.0.0rc1 (2026-04-22) through 1.9.0: `>=3.10,<3.15`.
  - The `alembic` extra first appears in 1.1.0, and needs Alembic ≥1.16 (≥1.18 from 1.8).
  - Alembic has required `>=3.10` since 1.17.0 (2025-10-11). 1.16.5 is the last release for 3.9.
- **`uv venv -p 3.9` and `uv pip install 'clickhouse-connect[alembic]==1.9.0'`:** unsatisfiable.
- **Unpinned `clickhouse-connect[alembic]` on 3.9 is a trap.** It quietly resolves to 0.15.1,
  which has no Alembic extra, and only warns. Any dependency on the extra needs a lower bound
  (`>=1.9`).
- **Forcing the 1.9.0 source onto 3.9** raises a deliberate `RuntimeError` at import
  (`clickhouse_connect/__init__.py:3-4`).
- **0.15.1 on 3.9** holds a session, but has no Alembic integration. Alembic can't create the
  version table there, and its version UPDATE fails ("Lightweight updates are not supported").
  Not usable.

## Q5. Other things that affect a migration tool

1. **Multi-statement strings** fail on both dialects ("Multi-statements are not allowed"). A
   trailing `;` is fine. This confirms that `run_sql` must split files itself.
2. **`:` read as a bind parameter.** `op.execute(str)` wraps the string in SQLAlchemy `text()`,
   so any `:word` after a non-word character is a bind parameter, on both dialects.
   - These fail: `SELECT ':abc'`, `SELECT 1 /* see :ref */`, `SELECT '{"a":1}'`.
   - These are fine: `toDateTime('2024-01-01 12:30:00')`, `1::String`, `'{x:String}'`, and `'\:abc'`.
   - **So `run_sql` must not go through `text()`.** Use the raw path (`exec_driver_sql`).
3. **The raw path differs by dialect:**
   - Legacy always applies Python `%`-formatting: `SELECT 'a%b'` raises, and `'%(x)s'` raises
     `KeyError`.
   - Official leaves `%` alone, except that it collapses `%%` to `%` when there are no
     parameters (`dbapi/cursor.py:338-344`).
   - **Doubling every `%` and sending no parameters gives the right result on both.**
4. **ON CLUSTER:**
   - `op.execute` of `ON CLUSTER` DDL works on both dialects, and a failing host raises.
   - Neither sets `distributed_ddl_task_timeout`.
   - The official Alembic integration has no `ON CLUSTER` support at all.
5. **The impl class:**
   - The official one is `clickhouse_connect.cc_sqlalchemy.alembic.impl.ClickHouseImpl`, with
     `__dialect__ = "clickhousedb"` and `transactional_ddl = False` (`impl.py:113-117`).
   - It registers only when `env.py` imports `clickhouse_connect.cc_sqlalchemy.alembic`.
     Otherwise Alembic silently falls back to the default impl.
   - Our `ClickhouseImpl` uses `"clickhouse"`, so it would not apply.
   - Subclassing the official impl works. Overriding `version_table_impl()` produced a
     replicated version table.
   - `_exec` sees every `op.execute` statement and Alembic's version-table writes. It does **not**
     see `op.get_bind().execute(...)` or the version-table CREATE.
   - **So 0.7's waiting must hook SQLAlchemy cursor events** (`before/after_cursor_execute`). Use
     `_exec` for rewriting version-table SQL.

## Not verified

- **ClickHouse Cloud:**
  - the version table's SharedMergeTree conversion;
  - `mutations_sync = 2` behaviour;
  - **whether Cloud routes every request carrying one session id to the same replica.** If it
    doesn't, the `SET` guarantee breaks. Run capability 3's integration test against a Cloud
    service before relying on it.
- Divergence across real multiple replicas.
- `session_timeout` passed through clickhouse-sqlalchemy's `ch_settings`.
- Both claims marked *Not tested* in Q2.

## Clean-environment reproduction (DRY-1391 validation)

Re-run on 2026-10-02 from the local `dy/ch-migrate-1.0` branch. This validates
Q1 and Q4 without switching the package's production dialect. The probe copies
the archived official/legacy environments and the original r001/r002 migrations
into disposable projects; the integration fixture starts and removes its own
ClickHouse container. No shared or Cloud service is contacted.

From the repository root:

```bash
scratch="$(mktemp -d)"
uv venv --python 3.10 "$scratch/py310"
uv pip install --python "$scratch/py310/bin/python" -e . \
  'clickhouse-connect[alembic]==1.9.0' 'clickhouse-sqlalchemy==0.3.2'
"$scratch/py310/bin/python" -c \
  'import clickhouse_connect.cc_sqlalchemy.alembic'
env -u CH_MIGRATE_IT_URL CH_MIGRATE_SPIKE_PYTHON="$scratch/py310/bin/python" \
  uv run --locked pytest -q -s -m integration \
  tests/integration/dialect_spike_probe.py

uv venv --python 3.9 "$scratch/py39"
# Expected to fail: this package range requires Python >=3.10,<3.15.
uv pip install --python "$scratch/py39/bin/python" 'clickhouse-connect[alembic]>=1.9'
rm -rf "$scratch"
```

The explicit probe filename is not collected by the regular unit or integration
suite. It requires the clean environment named above. Its credentials are
generated by the fixture and passed through the child environment, not printed.

Observed clean install: Python 3.10.18, clickhouse-connect 1.9.0
(`Requires-Python: >=3.10,<3.15`), clickhouse-sqlalchemy 0.3.2, Alembic 1.20.0,
SQLAlchemy 2.0.54. The official integration import succeeded.

```text
official: server_default=16; same_revision=1; next_revision=1
legacy: server_default=16; same_revision=16; next_revision=16
legacy_url: server_default=16; same_revision=16; next_revision=16
legacy_connect_args: server_default=16; same_revision=1; next_revision=1
4 passed
```

Python 3.9.6 refused the bounded official dependency:

```text
No solution found when resolving dependencies:
the current Python version (3.9.6) does not satisfy Python>=3.10,<3.15
and you require clickhouse-connect[alembic]>=1.9
```

The dated adoption decision above stands. Cloud routing and replicated
version-table behavior remain outside this reproduction and remain unverified
by it.
