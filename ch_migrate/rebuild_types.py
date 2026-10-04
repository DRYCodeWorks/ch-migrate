"""Shared online-rebuild values; credentials never enter persisted state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ch_migrate.introspect import TableDefinition
from ch_migrate.version_table import VersionTableState
from ch_migrate.waiting_types import WaitBudget


@dataclass(frozen=True)
class RebuildOptions:
    database: str
    table: str
    revision: str
    generation: int
    cluster: str | None = None
    select: str | None = None
    allow_unacknowledged_async_loss: bool = False


@dataclass(frozen=True)
class RebuildDefinition:
    database: str
    table: str
    source: TableDefinition
    target: TableDefinition
    create_sql: str
    projection: str
    columns: tuple[str, ...]
    cluster: str | None
    revision: str
    generation: int
    allow_unacknowledged_async_loss: bool = False


@dataclass(frozen=True)
class RebuildRuntime:
    client: Any
    control_factory: Callable[[], Any]
    definition: RebuildDefinition
    deployment: VersionTableState
    budget: WaitBudget
    record: dict
    checkpoint: Callable[[], None]
    refresh: Callable[[], dict | None]
