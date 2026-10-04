"""Durable, operator-released ClickHouse table rebuild."""

from __future__ import annotations

import sys
import threading
from uuid import uuid4

from ch_migrate.rebuild_ddl import dual_ddl, helper_ddl
from ch_migrate.rebuild_preflight import RebuildRequest, inspect_rebuild
from ch_migrate.waiting_ddl import DistributedDDLWaiter
from ch_migrate.waiting_types import UnknownOutcome, WaitingError

_HEARTBEAT_SECONDS = 2
_STALE_SECONDS = 8
_MAX_COPY_MEMORY = 1_073_741_824


def execute_rebuild(runtime) -> dict:
    """Run a rebuild; a failed owner retains its lock for operator reconciliation."""
    ctx = _Context(runtime)
    previous = runtime.record.get("rebuild")
    if previous and previous.get("result") is not None:
        _completed_without_lock(ctx)
        return previous["result"]
    if not previous:
        _preflight(ctx)
    with _Lock(ctx) as lock:
        refreshed = runtime.refresh()
        if refreshed is not None:
            runtime.record.clear()
            runtime.record.update(refreshed)
        state = runtime.record.get("rebuild")
        if state and state.get("result") is not None:
            _verify_complete(ctx)
            return state["result"]
        if not state:
            _preflight(ctx)
        _initialize(ctx)
        lock.check()
        _resolve_exchange(ctx)
        if not _swapped(ctx):
            _helpers(ctx)
            _dual(ctx)
            lock.check()
            _drain(ctx)
            _snapshot(ctx)
            _copy(ctx, lock)
            if ctx.definition.target.engine.startswith("Replicated"):
                _sync_replica(ctx, ctx.name("new"))
            _swap(ctx, lock)
        _cleanup(ctx, lock)
        _async_report(ctx)
        result = _result(ctx)
        ctx.state["result"] = result
        ctx.save()
        return result


class _Context:
    def __init__(self, runtime):
        self.runtime = runtime
        self.client = runtime.client
        self.definition = runtime.definition
        self.database = runtime.definition.database
        self.table = runtime.definition.table
        self.cluster = runtime.deployment.health_cluster
        self.ddl_cluster = runtime.definition.cluster
        self.budget = runtime.budget
        self.waiter = DistributedDDLWaiter(self.client, self.budget)
        self.hosts = self._hosts()

    @property
    def state(self):
        return self.runtime.record["rebuild"]

    def save(self):
        self.runtime.checkpoint()

    def name(self, role):
        return f"{self.table}__chm_{role}"

    def qualified(self, name):
        return f"{_id(self.database)}.{_id(name)}"

    def system(self, table):
        return (
            f"clusterAllReplicas({_lit(self.cluster)}, system.{table})"
            if self.cluster
            else f"system.{table}"
        )

    def rows(self, sql, parameters=None):
        self.budget.check("rebuild inspection")
        return self.client.query(
            sql, parameters=parameters or {}, settings={"skip_unavailable_shards": 0}
        ).result_rows

    def command(self, sql, settings=None):
        self.budget.check("rebuild server command")
        settings = dict(settings or {})
        if self.runtime.deployment.database_engine == "Replicated" and sql.startswith("CREATE "):
            # Query-scoped documented opt-ins preserve our journaled UUID and
            # isolated Keeper paths. Value 2 would silently replace them.
            settings.update(
                database_replicated_allow_explicit_uuid=1,
                database_replicated_allow_replicated_engine_arguments=1,
            )
        result = self.client.command(sql, settings=settings)
        self.budget.check("rebuild server command completion")
        return result

    def catalog(self, name):
        rows = self.rows(
            f"SELECT materialize(hostName()), toString(uuid), engine, comment FROM {self.system('tables')} "
            "WHERE database = {db:String} AND name = {name:String}",
            {"db": self.database, "name": name},
        )
        if len(rows) != len({str(row[0]) for row in rows}):
            raise WaitingError(f"Duplicate catalog host for {name}")
        return {
            str(host): (str(uid), str(engine), str(comment)) for host, uid, engine, comment in rows
        }

    def uuid_map(self, name, expected=None):
        catalog = self.catalog(name)
        if set(catalog) != self.hosts:
            raise WaitingError(f"Incomplete catalog for {name}: {catalog}")
        result = {host: values[0] for host, values in catalog.items()}
        expected_map = (
            expected if isinstance(expected, dict) else {host: expected for host in self.hosts}
        )
        if expected is not None and result != expected_map:
            raise UnknownOutcome(f"Table {name}: expected UUID {expected}, observed {result}")
        return result

    def converged(self, name, uid):
        catalog = self.catalog(name)
        expected = uid if isinstance(uid, dict) else {host: uid for host in self.hosts}
        return (
            set(catalog) == self.hosts
            and {host: row[0] for host, row in catalog.items()} == expected
        )

    def absent(self, name):
        return not self.catalog(name)

    def time(self):
        return str(self.rows("SELECT toString(now64(6))")[0][0])

    def wait(self, label, predicate):
        self.budget.check(label)
        while not predicate():
            self.budget.pause(label)
        self.budget.complete(label)
        self.budget.check(label)

    def ddl(self, key, sql, converged):
        """Never reissue a checkpointed statement after an ambiguous outcome."""
        operations = self.state.setdefault("ddl", {})
        intent = operations.get(key)
        fresh = intent is None
        if fresh:
            if converged():
                raise UnknownOutcome(f"Unowned pre-existing DDL result for {key}")
            intent = {"token": f"chm-rebuild-{uuid4()}", "acknowledged": False}
            if self.ddl_cluster:
                intent["receipt"] = self.waiter.prepare(self.ddl_cluster, intent["token"])
            operations[key] = intent
            self.save()
            self.command(
                sql,
                (
                    {"log_comment": intent["token"], "distributed_ddl_task_timeout": 0}
                    if self.ddl_cluster
                    else {"log_comment": intent["token"]}
                ),
            )
            intent["acknowledged"] = True
            self.save()
        if converged():
            intent["done"] = True
            self.save()
            return
        if self.ddl_cluster:
            self.waiter.wait(intent["receipt"], self.save)
            self.wait(f"DDL {key} replica catalogs", converged)
        elif fresh or intent["acknowledged"]:
            self.wait(f"DDL {key} replica catalogs", converged)
        else:
            raise UnknownOutcome(f"Local DDL {key} has no acknowledgement or catalog proof")
        intent["done"] = True
        self.save()

    def partition_ids(self, name):
        rows = self.rows(
            f"SELECT DISTINCT partition_id FROM {self.system('parts')} "
            "WHERE database = {db:String} AND table = {name:String} AND active AND rows > 0 ORDER BY partition_id",
            {"db": self.database, "name": name},
        )
        return sorted({str(row[0]) for row in rows})

    def counts(self, name, partition=None):
        table = self.qualified(name)
        source = f"clusterAllReplicas({_lit(self.cluster)}, {table})" if self.cluster else table
        where = " WHERE _partition_id = {part:String}" if partition is not None else ""
        rows = self.rows(
            f"SELECT materialize(hostName()) AS host, count() FROM {source}{where} GROUP BY host",
            {"part": partition} if partition is not None else {},
        )
        counts = {str(host): int(total) for host, total in rows}
        if not set(counts).issubset(self.hosts):
            raise WaitingError(f"Unexpected count hosts for {name}: {counts}")
        return {host: counts.get(host, 0) for host in self.hosts}

    def equal_count(self, name, partition, expected):
        return set(self.counts(name, partition).values()) == {expected}

    def local_count(self, name, partition):
        return int(
            self.rows(
                f"SELECT count() FROM {self.qualified(name)} "
                "WHERE _partition_id = {part:String}",
                {"part": partition},
            )[0][0]
        )

    def _hosts(self):
        hosts = [
            str(row[0])
            for row in self.rows(f"SELECT materialize(hostName()) FROM {self.system('one')}")
        ]
        if not hosts or len(hosts) != len(set(hosts)):
            raise WaitingError(f"Cannot prove distinct rebuild replicas: {hosts}")
        return set(hosts)


class _Lock:
    def __init__(self, context):
        self.ctx = context
        self.name = f"_chm_rebuild_lock_{context.table}"
        self.owner = str(uuid4())
        self.uuid = str(uuid4())
        self.stop = threading.Event()
        self.thread = None
        self.failure = None

    def __enter__(self):
        if not self.ctx.absent(self.name):
            self._refuse()
        try:
            self.ctx.command(
                self._create(), {"distributed_ddl_task_timeout": 0} if self.ctx.ddl_cluster else {}
            )
        except Exception as exc:
            if not self.ctx.absent(self.name):
                self._refuse()
            raise UnknownOutcome(
                f"Lock CREATE failed or outcome uncertain; inspect replicas before retry: {exc}"
            ) from exc
        self.ctx.wait("lock ownership on every replica", self._owned)
        self.check()
        self._beat()
        self.thread = threading.Thread(
            target=self._heartbeat, name="chm-rebuild-heartbeat", daemon=True
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=_HEARTBEAT_SECONDS + 3)
        if (
            exc_type is None
            and self.failure is None
            and self.thread is not None
            and not self.thread.is_alive()
        ):
            self.check()
            sql = f"DROP TABLE {self.ctx.qualified(self.name)}"
            if self.ctx.ddl_cluster:
                sql += f" ON CLUSTER {_id(self.ctx.ddl_cluster)}"
            self.ctx.command(sql + " SYNC")
            self.ctx.wait("owned lock release", lambda: self.ctx.absent(self.name))
        elif exc_type is None:
            raise WaitingError(
                f"Lock heartbeat failed or did not stop; lock retained: {self.failure}"
            )
        return False

    def _create(self):
        replicated = (
            len(self.ctx.hosts) > 1 or self.ctx.runtime.deployment.database_engine == "Replicated"
        )
        path = f"/clickhouse/tables/{{shard}}/chm-rebuild-lock/{self.ctx.database}/{self.name}/{self.uuid}"
        engine = (
            f"ReplicatedMergeTree({_lit(path)}, '{{replica}}')" if replicated else "MergeTree()"
        )
        cluster = f" ON CLUSTER {_id(self.ctx.ddl_cluster)}" if self.ctx.ddl_cluster else ""
        return (
            f"CREATE TABLE {self.ctx.qualified(self.name)} UUID {_lit(self.uuid)}{cluster} "
            "(owner String, heartbeat DateTime64(6) DEFAULT now64(6)) "
            f"ENGINE = {engine} ORDER BY heartbeat COMMENT {_lit('chm-rebuild-owner:' + self.owner)}"
        )

    def _owned(self):
        catalog = self.ctx.catalog(self.name)
        return set(catalog) == self.ctx.hosts and all(
            uid == self.uuid and comment == f"chm-rebuild-owner:{self.owner}"
            for uid, _, comment in catalog.values()
        )

    def check(self):
        if self.failure is not None or not self._owned():
            raise UnknownOutcome(f"Lock ownership lost or heartbeat stopped: {self.failure}")

    def _beat(self):
        control = self.ctx.runtime.control_factory()
        try:
            control.command(
                f"INSERT INTO {self.ctx.qualified(self.name)} (owner) "
                f"VALUES ({_lit(self.owner)})",
                settings={"async_insert": 0},
            )
        finally:
            if hasattr(control, "close"):
                control.close()

    def _heartbeat(self):
        while not self.stop.wait(_HEARTBEAT_SECONDS):
            try:
                self._beat()
            except Exception as exc:
                self.failure = exc
                return

    def _refuse(self):
        catalog = self.ctx.catalog(self.name)
        owners = {host: row[2] for host, row in catalog.items()}
        try:
            source = (
                f"clusterAllReplicas({_lit(self.ctx.cluster)}, {self.ctx.qualified(self.name)})"
                if self.ctx.cluster
                else self.ctx.qualified(self.name)
            )
            rows = self.ctx.rows(
                f"SELECT materialize(hostName()), max(heartbeat), "
                f"dateDiff('second', max(heartbeat), now64(6)) "
                f"FROM {source} GROUP BY hostName()"
            )
            heartbeat = {
                str(host): {
                    "last": str(last),
                    "age_seconds": int(age),
                    "stale": int(age) >= _STALE_SECONDS,
                }
                for host, last, age in rows
            }
        except Exception as exc:
            heartbeat = {"unavailable": str(exc)}
        raise WaitingError(
            f"Rebuild lock {self.ctx.qualified(self.name)} exists; owners={owners}; "
            f"heartbeat={heartbeat}. Expiry is diagnostic only; no automatic takeover. "
            "Operator: establish prior runner cannot resume; reconcile in-flight copy/DDL queries, "
            "waiting journal and all-host UUIDs; explicitly release lock on all replicas; rerun. "
            "This invocation will not release the lock."
        )


def _preflight(ctx):
    definition = ctx.definition
    if len(ctx.hosts) > 1 and not definition.source.engine.startswith(("Replicated", "Shared")):
        raise WaitingError(
            "Multi-host unreplicated source cannot use single-node physical partition copy"
        )
    request = RebuildRequest(
        definition.database,
        definition.source,
        definition.target,
        ctx.cluster,
        definition.allow_unacknowledged_async_loss,
        ((definition.source, definition.source), (definition.target, definition.target)),
    )
    findings = inspect_rebuild(ctx.client, request).findings
    for finding in findings:
        if finding.severity == "warning":
            _progress(f"Warning [{finding.code}]: {finding.message} {finding.details}")
    refusals = [
        f"{finding.code}: {finding.message}"
        for finding in findings
        if finding.severity == "refusal"
    ]
    if refusals:
        raise WaitingError("Rebuild read-only preflight refused: " + "; ".join(refusals))


def _initialize(ctx):
    record = ctx.runtime.record
    state = record.get("rebuild")
    if state and state.get("helpers"):
        if not record.get("source_ddl"):
            raise UnknownOutcome("Original source_ddl missing; cannot safely resume post-swap")
        return
    old = ctx.uuid_map(ctx.table)
    names = [ctx.name(role) for role in ("new", "snap", "stage", "dual")]
    rollback = f"{ctx.table}__chm_old_{ctx.definition.revision}"
    for name in names + [rollback]:
        if not ctx.absent(name):
            raise WaitingError(f"Unowned helper exists: {ctx.qualified(name)}")
    state = record.setdefault("rebuild", {})
    state.update(
        {
            "helpers": {role: str(uuid4()) for role in ("new", "snap", "stage")},
            "old_uuids": old,
            "rollback_table": rollback,
            "snapshot": {},
            "parts": {},
            "ddl": {},
        }
    )
    ctx.save()


def _helpers(ctx):
    for role in ("new", "snap", "stage"):
        name = ctx.name(role)
        uid = ctx.state["helpers"][role]
        ctx.ddl(
            f"create_{role}",
            helper_ddl(ctx.definition, role, uid),
            lambda n=name, u=uid: ctx.converged(n, u),
        )
    _stop_merges(ctx, ctx.name("stage"))
    _stop_merges(ctx, ctx.name("snap"))


def _stop_merges(ctx, name):
    cluster = f" ON CLUSTER {_id(ctx.cluster)}" if ctx.cluster else ""
    ctx.command(f"SYSTEM STOP MERGES{cluster} {ctx.qualified(name)}")


def _dual(ctx):
    state = ctx.state
    name = ctx.name("dual")
    if "dual_start" not in state:
        state["dual_start"] = ctx.time()
        ctx.save()
    if "dual_uuid" not in state:
        if not ctx.absent(name):
            raise UnknownOutcome(f"Unowned dual view exists: {ctx.catalog(name)}")
        state["dual_uuid"] = str(uuid4())
        ctx.save()
    uid = state["dual_uuid"]
    ctx.ddl("create_dual", dual_ddl(ctx.definition, uid), lambda: ctx.converged(name, uid))
    ctx.wait("dual visible on every replica", lambda: ctx.converged(name, uid))
    if "barrier" not in state:
        state["barrier"] = ctx.time()
        ctx.save()


def _drain(ctx):
    if ctx.state.get("drained"):
        return
    _drain_before(ctx, ctx.state["barrier"])
    if ctx.definition.source.engine.startswith("Replicated"):
        _sync_source(ctx)
    ctx.state["drained"] = True
    ctx.save()


def _drain_before(ctx, barrier):
    sql = (
        "SELECT materialize(hostName()), query_id FROM "
        + ctx.system("processes")
        + " WHERE query_kind IN ('Insert', 'AsyncInsertFlush') "
        "AND now64(6) - toIntervalMillisecond(toUInt64(elapsed * 1000)) "
        "<= toDateTime64({barrier:String}, 6)"
    )
    ctx.wait("pre-barrier insert pipelines", lambda: not ctx.rows(sql, {"barrier": barrier}))


def _sync_source(ctx):
    _sync_replica(ctx, ctx.table)


def _sync_replica(ctx, name):
    cluster = f" ON CLUSTER {_id(ctx.cluster)}" if ctx.cluster else ""
    # LIGHTWEIGHT drains the captured fetch queue without requiring future
    # inserts or background merges to stop.
    ctx.command(f"SYSTEM SYNC REPLICA{cluster} {ctx.qualified(name)} LIGHTWEIGHT")


def _snapshot(ctx):
    snapshot = ctx.state["snapshot"]
    if snapshot.get("complete"):
        return
    if snapshot.get("started"):
        if any(part.get("state") == "moved" for part in ctx.state["parts"].values()):
            raise UnknownOutcome("Incomplete snapshot after moved partition; reconcile manually")
        for partition in ctx.partition_ids(ctx.name("snap")):
            _drop_partition(ctx, ctx.name("snap"), partition)
        snapshot.clear()
        ctx.save()
    _stop_merges(ctx, ctx.name("snap"))
    snapshot.update({"partitions": ctx.partition_ids(ctx.table), "started": True, "counts": {}})
    ctx.save()
    for partition in snapshot["partitions"]:
        ctx.command(
            f"ALTER TABLE {ctx.qualified(ctx.name('snap'))} "
            f"ATTACH PARTITION ID {_lit(partition)} FROM {ctx.qualified(ctx.table)}"
        )
        expected = ctx.local_count(ctx.name("snap"), partition)
        ctx.wait(
            f"snapshot {partition} replicated",
            lambda p=partition, n=expected: ctx.equal_count(ctx.name("snap"), p, n),
        )
        snapshot["counts"][partition] = expected
        ctx.save()
    snapshot["complete"] = True
    snapshot["end"] = ctx.time()
    ctx.save()


def _copy(ctx, lock):
    _stop_merges(ctx, ctx.name("stage"))
    for partition, expected in sorted(ctx.state["snapshot"]["counts"].items()):
        lock.check()
        _copy_partition(ctx, partition, expected)


def _copy_partition(ctx, partition, expected):
    item = ctx.state["parts"].setdefault(partition, {})
    if item.get("state") == "moved":
        _progress(f"Skipping previously moved partition {partition}")
        return
    stage = ctx.name("stage")
    if expected == 0:
        if not ctx.equal_count(stage, partition, 0):
            raise UnknownOutcome(f"Nonempty stage for empty snapshot partition {partition}")
        item["state"] = "moved"
        ctx.save()
        _progress(f"Skipping empty snapshot partition {partition}")
        return
    other = set(ctx.partition_ids(stage)) - {partition}
    if other:
        raise UnknownOutcome(
            f"Stage has unexpected partition IDs {sorted(other)}; projection may change partition key"
        )
    counts = ctx.counts(stage, partition)
    if item.get("state") == "staged" and item.get("move_intent"):
        observed = set(counts.values())
        if not observed.issubset({0, expected}):
            raise UnknownOutcome(f"Partial MOVE for {partition}: {counts}; reconcile before retry")
        if observed == {0, expected}:
            ctx.wait(
                f"prior MOVE {partition} replica convergence",
                lambda: ctx.equal_count(stage, partition, 0),
            )
            counts = ctx.counts(stage, partition)
    if item.get("state") == "staged" and set(counts.values()) == {0}:
        _progress(f"Recovering MOVE for staged partition {partition}")
        item["state"] = "moved"
        ctx.save()
        return
    if item.get("state") != "staged" or set(counts.values()) != {expected}:
        _progress(f"Recovering/copying partition {partition}")
        _kill_orphan(ctx, _copy_id(ctx, partition))
        _drop_partition(ctx, stage, partition)
        item["state"] = "copying"
        ctx.save()
        columns = ", ".join(_id(name) for name in ctx.definition.columns)
        sql = (
            f"INSERT INTO {ctx.qualified(stage)} ({columns}) "
            f"SELECT {ctx.definition.projection} FROM {ctx.qualified(ctx.name('snap'))} "
            f"WHERE _partition_id = {_lit(partition)}"
        )
        ctx.command(
            sql,
            {
                "query_id": _copy_id(ctx, partition),
                "max_threads": 2,
                "max_insert_threads": 1,
                "max_memory_usage": _MAX_COPY_MEMORY,
                "max_block_size": 1024,
                "async_insert": 0,
            },
        )
        _verify_stage(ctx, partition, expected)
        item.update({"state": "staged", "rows": expected})
        ctx.save()
    _verify_stage(ctx, partition, expected)
    item["move_intent"] = True
    ctx.save()
    ctx.command(
        f"ALTER TABLE {ctx.qualified(stage)} MOVE PARTITION ID {_lit(partition)} "
        f"TO TABLE {ctx.qualified(ctx.name('new'))}"
    )
    ctx.wait(f"stage partition {partition} moved", lambda: ctx.equal_count(stage, partition, 0))
    item["state"] = "moved"
    ctx.save()
    _progress(f"Moved partition {partition}: {expected} snapshot rows")


def _verify_stage(ctx, partition, expected):
    def complete():
        ids = set(ctx.partition_ids(ctx.name("stage")))
        if ids - {partition}:
            raise UnknownOutcome(
                f"Projection changed partition ID for {partition}: stage has {sorted(ids)}"
            )
        return ctx.equal_count(ctx.name("stage"), partition, expected)

    ctx.wait(f"stage {partition} count and replica convergence", complete)


def _kill_orphan(ctx, query_id):
    sql = "KILL QUERY"
    if ctx.cluster:
        sql += f" ON CLUSTER {_id(ctx.cluster)}"
    ctx.command(sql + f" WHERE query_id = {_lit(query_id)} SYNC")
    ctx.wait(
        f"orphan copy {query_id} stopped",
        lambda: not ctx.rows(
            f"SELECT materialize(hostName()) FROM {ctx.system('processes')} "
            "WHERE query_id = {qid:String}",
            {"qid": query_id},
        ),
    )


def _drop_partition(ctx, table, partition):
    ctx.command(f"ALTER TABLE {ctx.qualified(table)} DROP PARTITION ID {_lit(partition)}")
    ctx.wait(
        f"drop {table} partition {partition} propagated",
        lambda: ctx.equal_count(table, partition, 0),
    )


def _copy_id(ctx, partition):
    return f"chm-rebuild-{ctx.definition.revision}-{ctx.database}.{ctx.table}-{partition}"


def _exchange_evidence(ctx):
    old = ctx.state["old_uuids"]
    new = ctx.state["helpers"]["new"]
    catalog = ctx.catalog(ctx.table)
    if set(catalog) != ctx.hosts:
        raise UnknownOutcome(f"Source catalog incomplete: {catalog}")
    observed = {host: row[0] for host, row in catalog.items()}
    if any(uid not in {new, old.get(host)} for host, uid in observed.items()):
        raise UnknownOutcome(f"Foreign source UUIDs during rebuild: {observed}")
    if set(observed.values()) == {new}:
        return "swapped"
    if observed == old:
        return "old"
    return "mixed"


def _swapped(ctx):
    outcome = _exchange_evidence(ctx)
    if outcome == "mixed":
        raise UnknownOutcome(f"Mixed exchange UUIDs: {ctx.catalog(ctx.table)}")
    return outcome == "swapped"


def _resolve_exchange(ctx):
    intent = ctx.state.get("ddl", {}).get("exchange")
    if intent is None:
        if _exchange_evidence(ctx) == "mixed":
            raise UnknownOutcome("Mixed UUIDs with no owned EXCHANGE receipt")
        return
    if _exchange_evidence(ctx) == "swapped":
        return
    old = ctx.state["old_uuids"]
    new = ctx.state["helpers"]["new"]
    if ctx.ddl_cluster:
        ctx.waiter.wait(intent["receipt"], ctx.save)
        ctx.wait(
            "EXCHANGE catalog convergence",
            lambda: ctx.converged(ctx.table, new) and ctx.converged(ctx.name("new"), old),
        )
    elif intent.get("acknowledged"):
        ctx.wait(
            "EXCHANGE catalog convergence",
            lambda: ctx.converged(ctx.table, new) and ctx.converged(ctx.name("new"), old),
        )
    else:
        raise UnknownOutcome("Unacknowledged EXCHANGE cannot be retried without catalog proof")
    intent["done"] = True
    ctx.save()


def _swap(ctx, lock):
    state = ctx.state
    old = state["old_uuids"]
    new = state["helpers"]["new"]
    if "swap_intent" not in state:
        state["swap_intent"] = ctx.time()
        ctx.save()
    lock.check()
    if not _swapped(ctx):
        _flush_async(ctx)
        sql = f"EXCHANGE TABLES {ctx.qualified(ctx.table)} AND {ctx.qualified(ctx.name('new'))}"
        if ctx.ddl_cluster:
            sql += f" ON CLUSTER {_id(ctx.ddl_cluster)}"
        ctx.ddl(
            "exchange",
            sql,
            lambda: ctx.converged(ctx.table, new) and ctx.converged(ctx.name("new"), old),
        )
    ctx.wait(
        "new table UUID on every replica",
        lambda: ctx.converged(ctx.table, new) and ctx.converged(ctx.name("new"), old),
    )
    state["exchanged"] = True
    ctx.save()


def _cleanup(ctx, lock):
    state = ctx.state
    if not _swapped(ctx):
        raise UnknownOutcome("Cleanup refused without all-host new UUID")
    old = state["old_uuids"]
    lock.check()
    dual = ctx.name("dual")
    sql = f"DROP TABLE {ctx.qualified(dual)}"
    if ctx.ddl_cluster:
        sql += f" ON CLUSTER {_id(ctx.ddl_cluster)}"
    ctx.ddl("drop_dual", sql + " SYNC", lambda: ctx.absent(dual))
    ctx.wait("dual DROP SYNC on every replica", lambda: ctx.absent(dual))
    # Catalog absence alone does not prove an unacknowledged DROP SYNC finished
    # draining pipelines. Re-establish that barrier across every replica.
    if not state.get("dual_drained"):
        if "dual_drop_barrier" not in state:
            state["dual_drop_barrier"] = ctx.time()
            ctx.save()
        _drain_before(ctx, state["dual_drop_barrier"])
        state["dual_drained"] = True
        ctx.save()
    rollback = state["rollback_table"]
    if not ctx.converged(rollback, old):
        ctx.uuid_map(ctx.name("new"), old)
        sql = f"RENAME TABLE {ctx.qualified(ctx.name('new'))} TO {ctx.qualified(rollback)}"
        if ctx.ddl_cluster:
            sql += f" ON CLUSTER {_id(ctx.ddl_cluster)}"
        ctx.ddl(
            "rename_old", sql, lambda: ctx.converged(rollback, old) and ctx.absent(ctx.name("new"))
        )
    ctx.uuid_map(rollback, old)
    for role in ("snap", "stage"):
        name = ctx.name(role)
        sql = f"DROP TABLE {ctx.qualified(name)}"
        if ctx.ddl_cluster:
            sql += f" ON CLUSTER {_id(ctx.ddl_cluster)}"
        ctx.ddl(f"drop_{role}", sql + " SYNC", lambda n=name: ctx.absent(n))
    state["cleanup"] = True
    ctx.save()


def _async_report(ctx):
    state = ctx.state
    if "async_flush_errors" in state:
        return
    if not state.get("swap_intent"):
        raise UnknownOutcome("Swap timestamp missing; cannot attribute async flush errors")
    logs = "SYSTEM FLUSH LOGS"
    if ctx.cluster:
        logs += f" ON CLUSTER {_id(ctx.cluster)}"
    if "report_end" not in state:
        # Queues follow the old storage UUID. Flushing the now-live name would
        # flush the replacement and miss the old queue's later error 741.
        _flush_async(ctx, state["rollback_table"])
        _drain_before(ctx, ctx.time())
        ctx.command(logs)
        state["report_end"] = ctx.time()
        ctx.save()
    ctx.command(logs)
    _require_async_log(ctx)
    old = sorted(set(state["old_uuids"].values()))
    rows = ctx.rows(
        f"SELECT materialize(hostName()), query_id, bytes FROM {ctx.system('asynchronous_insert_log')} "
        "WHERE database = {db:String} AND table = {table:String} AND status = 'FlushError' "
        "AND arrayExists(uuid -> position(exception, uuid) > 0, {old:Array(String)}) "
        "AND flush_time_microseconds >= toDateTime64({start:String}, 6) "
        "AND flush_time_microseconds <= toDateTime64({end:String}, 6)",
        {
            "db": ctx.database,
            "table": ctx.table,
            "old": old,
            "start": state["swap_intent"],
            "end": state["report_end"],
        },
    )
    state["async_flush_errors"] = {
        "count": len(rows),
        "bytes": sum(int(row[2]) for row in rows),
        "query_ids": sorted({str(row[1]) for row in rows}),
    }
    ctx.save()
    _progress(f"Old-UUID asynchronous FlushErrors: {state['async_flush_errors']}")


def _require_async_log(ctx):
    columns = {
        str(row[0])
        for row in ctx.rows(
            "SELECT name FROM system.columns WHERE database = 'system' AND table = 'asynchronous_insert_log'"
        )
    }
    required = {
        "database",
        "table",
        "status",
        "bytes",
        "query_id",
        "exception",
        "flush_time_microseconds",
    }
    if not required.issubset(columns):
        raise WaitingError(f"Async log lacks old-UUID evidence: {sorted(required - columns)}")


def _flush_async(ctx, table=None):
    cluster = f" ON CLUSTER {_id(ctx.cluster)}" if ctx.cluster else ""
    ctx.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE{cluster} {ctx.qualified(table or ctx.table)}")


def _result(ctx):
    state = ctx.state
    old = state["old_uuids"]
    return {
        "old_uuids": ctx.uuid_map(state["rollback_table"], old),
        "new_uuids": ctx.uuid_map(ctx.table, state["helpers"]["new"]),
        "rollback_table": state["rollback_table"],
        "duplicate_window": {"start": state["dual_start"], "end": state["snapshot"]["end"]},
        "async_flush_errors": state["async_flush_errors"],
    }


def _completed_without_lock(ctx):
    if not ctx.absent(f"_chm_rebuild_lock_{ctx.table}"):
        raise WaitingError(
            "Completed rebuild has a retained lock; operator reconciliation/release required"
        )
    _verify_complete(ctx)


def _verify_complete(ctx):
    state = ctx.state
    ctx.uuid_map(ctx.table, state["helpers"]["new"])
    ctx.uuid_map(state["rollback_table"], state["old_uuids"])
    for role in ("dual", "snap", "stage"):
        if not ctx.absent(ctx.name(role)):
            raise UnknownOutcome(f"Completed rebuild has surviving {role} helper")


def _progress(message):
    print(f"Rebuild: {message}", file=sys.stderr, flush=True)


def _id(value):
    return "`" + value.replace("`", "``") + "`"


def _lit(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
