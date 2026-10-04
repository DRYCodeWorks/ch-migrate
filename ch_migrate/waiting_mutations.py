"""Mutation ownership, predecessor handling, and per-replica completion evidence."""

from __future__ import annotations

import re

from ch_migrate.waiting_sql import sql_string
from ch_migrate.waiting_types import UnknownOutcome, WaitingError


class MutationWaiter:
    def __init__(self, client, state, budget):
        self.client, self.state, self.budget = client, state, budget

    def target(self, table: tuple[str, str]) -> dict | None:
        rows = self.client.query(
            "SELECT materialize(hostName()), toString(uuid), engine, sorting_key FROM system.tables "
            "WHERE database = {db:String} AND name = {table:String}",
            parameters={"db": table[0], "table": table[1]},
        ).result_rows
        if not rows:
            return None
        host, uuid, engine, sorting_key = rows[0]
        target = {
            "database": table[0],
            "table": table[1],
            "engine": engine,
            "initiator": host,
            "uuids": {host: uuid},
            "cluster": None,
            "sorting_key": sorting_key,
        }
        if engine.startswith("Shared"):
            target["initiator"], target["uuids"] = "shared", {"shared": uuid}
        elif engine.startswith("Replicated"):
            self._replicated_target(target)
        return target

    def prepare(self, target: dict, token: str) -> dict:
        while True:
            rows = self._rows(target)
            pending = [row for row in rows if not row[3]]
            self._failures(target, pending, "Blocked by foreign mutation")
            if not pending:
                self.budget.complete(f"{target['database']}.{target['table']} foreign predecessors")
                break
            self.budget.pause("foreign predecessor " + self._progress(target, pending))
        target = dict(target)
        target.update(token=token, ids={}, done_hosts=[], baseline={})
        for host in target["uuids"]:
            target["baseline"][host] = sorted({row[1] for row in rows if row[0] == host})
        return target

    def wait(self, receipt: dict, checkpoint) -> None:
        while True:
            identities = self._identities(receipt)
            self._visible_hosts = set(identities)
            for host, uuid in identities.items():
                if host in receipt["uuids"] and uuid != receipt["uuids"][host]:
                    raise UnknownOutcome(
                        f"Table {receipt['database']}.{receipt['table']} was replaced on {host}; statement not reissued"
                    )
            if receipt["initiator"] not in identities and not receipt.get("cluster"):
                raise UnknownOutcome(
                    f"Table {receipt['database']}.{receipt['table']} disappeared; statement not reissued"
                )
            rows = self._rows(receipt)
            self._bind_markers(receipt, rows, checkpoint)
            pending = self._pending(receipt, rows, checkpoint)
            if not pending:
                self.budget.complete(
                    f"{receipt['database']}.{receipt['table']}; every required replica finished"
                )
                return
            self.budget.pause("; ".join(pending))

    def _replicated_target(self, target: dict) -> None:
        path, total = self.client.query(
            "SELECT zookeeper_path, total_replicas FROM system.replicas "
            "WHERE database = {db:String} AND table = {table:String}",
            parameters=self._parameters(target),
        ).result_rows[0]
        cluster = self.state.health_cluster
        if not cluster:
            if total > 1:
                raise WaitingError(
                    "Replicated mutation waiting requires an environment cluster covering every replica"
                )
            return
        target["cluster"] = cluster
        while True:
            replicas = self.client.query(
                "SELECT materialize(hostName()), replica_name FROM "
                "clusterAllReplicas({cluster:String}, system.replicas) "
                "WHERE database = {db:String} AND table = {table:String} AND zookeeper_path = {path:String}",
                parameters={**self._parameters(target), "cluster": cluster, "path": path},
                settings=self._settings(),
            ).result_rows
            if len({replica for _, replica in replicas}) == total:
                hosts = {host for host, _ in replicas}
                target["uuids"] = {
                    host: uuid for host, uuid in self._identities(target).items() if host in hosts
                }
                if len(target["uuids"]) == total:
                    return
            self.budget.pause(
                f"{target['database']}.{target['table']}: cluster {cluster} exposes {len(replicas)}/{total} required replicas"
            )

    def _bind_markers(self, receipt: dict, rows: list, checkpoint) -> None:
        found = False
        for host in receipt["uuids"]:
            if host in receipt["done_hosts"] or host in receipt["ids"]:
                found = True
                continue
            local = [row for row in rows if row[0] == host]
            markers = {row[1] for row in local if receipt["token"] in row[2]}
            if not markers:
                continue
            if len(markers) != receipt.get("expected_markers", 1):
                raise UnknownOutcome(
                    f"Outcome unknown for partially submitted batch on {host}; statement not reissued"
                )
            found = True
            barrier = max(_order(value) for value in markers)
            baseline = set(receipt["baseline"].get(host, []))
            receipt["ids"][host] = sorted(
                markers
                | {
                    row[1]
                    for row in local
                    if not row[3] and row[1] not in baseline and _order(row[1]) <= barrier
                }
            )
            checkpoint()
        if not found:
            raise UnknownOutcome(
                f"Outcome unknown for {receipt['database']}.{receipt['table']} token {receipt['token']}: "
                "ownership evidence is missing or expired; statement not reissued; migration incomplete. "
                "Stop runners and reconcile the journal with independent server evidence."
            )

    def _pending(self, receipt: dict, rows: list, checkpoint) -> list[str]:
        pending = []
        for host in receipt["uuids"]:
            if host in receipt["done_hosts"]:
                continue
            ids = receipt["ids"].get(host)
            if not ids:
                pending.append(
                    f"{receipt['database']}.{receipt['table']}: host {host} has not supplied the owned mutation"
                )
                continue
            local = [row for row in rows if row[0] == host and row[1] in ids]
            missing = set(ids) - {row[1] for row in local}
            if missing:
                if host not in self._visible_hosts:
                    pending.append(
                        f"{receipt['database']}.{receipt['table']}: host {host} missing mutation evidence {sorted(missing)}"
                    )
                    continue
                raise UnknownOutcome(
                    f"Outcome unknown: mutation evidence {sorted(missing)} disappeared on {host}; statement not reissued"
                )
            self._failures(receipt, local, "Mutation or preceding work failed")
            unfinished = [row for row in local if not row[3]]
            if unfinished:
                pending.append(self._progress(receipt, unfinished))
            else:
                receipt["done_hosts"].append(host)
                checkpoint()
        return pending

    def _rows(self, target: dict) -> list:
        source, parameters = self._source(target, "mutations")
        rows = self.client.query(
            f"SELECT materialize(hostName()), mutation_id, command, is_done, parts_to_do, latest_fail_reason "
            f"FROM {source} WHERE database = {{db:String}} AND table = {{table:String}}",
            parameters=parameters,
            settings=self._settings() if target.get("cluster") else None,
        ).result_rows
        if target["engine"].startswith("Shared"):
            return [("shared", *row[1:]) for row in rows]
        return [row for row in rows if row[0] in target["uuids"]]

    def _identities(self, target: dict) -> dict[str, str]:
        source, parameters = self._source(target, "tables")
        rows = self.client.query(
            f"SELECT materialize(hostName()), toString(uuid) FROM {source} "
            "WHERE database = {db:String} AND name = {table:String}",
            parameters=parameters,
            settings=self._settings() if target.get("cluster") else None,
        ).result_rows
        if target["engine"].startswith("Shared"):
            return {"shared": rows[0][1]} if rows else {}
        return dict(rows)

    def _source(self, target: dict, table: str) -> tuple[str, dict]:
        parameters = self._parameters(target)
        if target.get("cluster"):
            parameters["cluster"] = target["cluster"]
            return f"clusterAllReplicas({{cluster:String}}, system.{table})", parameters
        return f"system.{table}", parameters

    def _settings(self) -> dict:
        return {
            "skip_unavailable_shards": 1,
            "connect_timeout_with_failover_ms": 250,
            "receive_timeout": 2,
        }

    @staticmethod
    def _parameters(target: dict) -> dict:
        return {"db": target["database"], "table": target["table"]}

    @staticmethod
    def _failures(target: dict, rows: list, prefix: str) -> None:
        for host, mutation, _, done, _, reason in rows:
            if not done and reason:
                kill = (
                    f"KILL MUTATION WHERE database = {sql_string(target['database'])} "
                    f"AND table = {sql_string(target['table'])} AND mutation_id = {sql_string(mutation)}"
                )
                raise WaitingError(
                    f"{prefix}: {target['database']}.{target['table']} mutation {mutation} on {host}: {reason}\nOperator only on {host}: {kill}"
                )

    @staticmethod
    def _progress(target: dict, rows: list) -> str:
        parts = {}
        for host, mutation, _, _, count, _ in rows:
            parts[(host, mutation)] = max(parts.get((host, mutation), 0), count)
        return "; ".join(
            f"{target['database']}.{target['table']} mutation {mutation} host {host}; parts_to_do={count}"
            for (host, mutation), count in sorted(parts.items())
        )


def _order(mutation: str) -> int:
    numbers = re.findall(r"\d+", mutation)
    if len(numbers) != 1:
        raise WaitingError(f"Unrecognized mutation ordering identifier: {mutation}")
    return int(numbers[0])
