"""Append-only waiting receipts, with a fresh generation after each downgrade."""

from __future__ import annotations

import json
from dataclasses import dataclass

from clickhouse_connect.cc_sqlalchemy.datatypes.sqltypes import UInt32, UInt64
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.schema import CreateTable

from ch_migrate.version_table import migration_state_engine
from ch_migrate.waiting_sql import qualified_table, query_settings
from ch_migrate.waiting_types import UnknownOutcome

JOURNAL_TABLE = "_ch_migrate_journal"


@dataclass(frozen=True)
class StepKey:
    revision: str
    generation: int
    position: int


class WaitingJournal:
    """One ordered writer per database; readers synchronize before resuming."""

    def __init__(self, connection, state, budget):
        self.connection, self.state, self.budget = connection, state, budget
        self.client = connection.connection.dbapi_connection.client
        self.table = qualified_table((state.database, JOURNAL_TABLE))
        self._sequences: dict[StepKey, int] = {}
        self._ready = False

    def exists(self) -> bool:
        return bool(
            self.client.query(
                "SELECT count() FROM system.tables WHERE database = {db:String} AND name = {table:String}",
                parameters={"db": self.state.database, "table": JOURNAL_TABLE},
            ).result_rows[0][0]
        )

    def synchronize(self) -> None:
        if not self.exists():
            return
        engine = self.client.query(
            "SELECT engine FROM system.tables WHERE database = {db:String} AND name = {table:String}",
            parameters={"db": self.state.database, "table": JOURNAL_TABLE},
        ).result_rows[0][0]
        if engine.startswith(("Replicated", "Shared")):
            self.client.command(f"SYSTEM SYNC REPLICA {self.table} LIGHTWEIGHT")

    def begin(self, revision: str, fingerprint: str) -> int:
        self.ensure()
        generation = self._generation(revision)
        if generation == 0:
            generation = 1
            self.write(StepKey(revision, generation, 0), {"fingerprint": fingerprint})
        else:
            control = self.read(StepKey(revision, generation, 0))
            if control is None:
                raise UnknownOutcome(
                    f"Journal generation for {revision} has no control record; reconcile before resuming"
                )
        return generation

    def invalidate(self, revision: str, fingerprint: str) -> None:
        if self.exists():
            self.write(
                StepKey(revision, self._generation(revision) + 1, 0), {"fingerprint": fingerprint}
            )

    def records(self, revision: str, generation: int) -> list[tuple[StepKey, dict]]:
        rows = self.client.query(
            f"SELECT position, max(sequence), argMax(payload, sequence) FROM {self.table} "
            "WHERE revision = {revision:String} AND generation = {generation:UInt64} AND position > 0 "
            "GROUP BY position ORDER BY position",
            parameters={"revision": revision, "generation": generation},
        ).result_rows
        result = []
        for position, sequence, payload in rows:
            key = StepKey(revision, generation, position)
            self._sequences[key] = sequence
            result.append((key, json.loads(payload)))
        return result

    def read(self, key: StepKey) -> dict | None:
        rows = self.client.query(
            f"SELECT sequence, payload FROM {self.table} WHERE revision = {{revision:String}} "
            "AND generation = {generation:UInt64} AND position = {position:UInt32} "
            "ORDER BY sequence DESC LIMIT 2",
            parameters=self._parameters(key),
        ).result_rows
        if not rows:
            self._sequences[key] = 0
            return None
        if len(rows) == 2 and rows[0][0] == rows[1][0] and rows[0][1] != rows[1][1]:
            raise UnknownOutcome(
                f"Conflicting journal writers for {key.revision}:{key.position}; serialize runners and reconcile"
            )
        self._sequences[key] = rows[0][0]
        return json.loads(rows[0][1])

    def write(self, key: StepKey, payload: dict) -> None:
        if key not in self._sequences:
            self.read(key)
        sequence = self._sequences[key] + 1
        self.client.insert(
            f"{self.state.database}.{JOURNAL_TABLE}",
            [
                [
                    key.revision,
                    key.generation,
                    key.position,
                    sequence,
                    json.dumps(payload, sort_keys=True),
                ]
            ],
            column_names=["revision", "generation", "position", "sequence", "payload"],
            settings={"async_insert": 0},
        )
        self._sequences[key] = sequence

    def unfinished_scope(self, revision: str) -> bool:
        if not self.exists():
            return False
        generation = self._generation(revision)
        return any(record["phase"] != "done" for _, record in self.records(revision, generation))

    def unfinished_versions(self) -> list[tuple[StepKey, dict]]:
        if not self.exists():
            return []
        self.synchronize()
        rows = self.client.query(
            f"SELECT revision, generation, position, max(sequence), argMax(payload, sequence) AS latest "
            f"FROM {self.table} WHERE position > 0 AND (revision, generation) IN "
            f"(SELECT revision, max(generation) FROM {self.table} GROUP BY revision) "
            "GROUP BY revision, generation, position HAVING JSONExtractString(latest, 'kind') = 'version' "
            "AND JSONExtractString(latest, 'phase') != 'done' ORDER BY revision, position"
        ).result_rows
        result = []
        for revision, generation, position, sequence, payload in rows:
            key = StepKey(revision, generation, position)
            self._sequences[key] = sequence
            result.append((key, json.loads(payload)))
        return result

    def ensure(self) -> None:
        if self._ready:
            return
        if self.exists():
            self.synchronize()
            self._ready = True
            return
        table = Table(
            JOURNAL_TABLE,
            MetaData(),
            Column("revision", String, nullable=False),
            Column("generation", UInt64, nullable=False),
            Column("position", UInt32, nullable=False),
            Column("sequence", UInt64, nullable=False),
            Column("payload", String, nullable=False),
            migration_state_engine(
                self.state, JOURNAL_TABLE, "(revision, generation, position, sequence)"
            ),
            schema=self.state.database,
            info={"ch_migrate_on_cluster": self.state.on_cluster},
        )
        ddl = str(CreateTable(table, if_not_exists=True).compile(dialect=self.connection.dialect))
        self.client.command(query_settings(ddl, {"distributed_ddl_task_timeout": "0"}))
        while not self.exists():
            self.budget.pause(f"journal {self.table} metadata is not available on this host")
        self.synchronize()
        self._ready = True

    def _generation(self, revision: str) -> int:
        return self.client.query(
            f"SELECT max(generation) FROM {self.table} WHERE revision = {{revision:String}}",
            parameters={"revision": revision},
        ).result_rows[0][0]

    @staticmethod
    def _parameters(key: StepKey) -> dict:
        return {"revision": key.revision, "generation": key.generation, "position": key.position}
