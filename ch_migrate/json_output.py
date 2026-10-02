"""Versioned CLI documents; stdout contains only JSON in machine-readable mode."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from typing import Any, NoReturn

import click

from ch_migrate.diff import DiffStatus, SchemaDiff
from ch_migrate.downgrade import irreversible_reason
from ch_migrate.lint import GATE_RULES, LintReport, Severity
from ch_migrate.rebase import RevisionGraph


class JsonCommand(click.Command):
    """Keep diagnostics off stdout and return a document for command errors."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        json_requested = "--json" in args
        try:
            return super().parse_args(ctx, args)
        except click.ClickException as error:
            if not json_requested:
                raise
            emit_json(self.name or "", {"error": error.format_message()})
            raise click.exceptions.Exit(2) from error

    def invoke(self, ctx: click.Context) -> Any:
        if not ctx.params.get("json_output"):
            return super().invoke(ctx)
        ctx.meta["json_stdout"] = sys.stdout
        with redirect_stdout(sys.stderr):
            try:
                return super().invoke(ctx)
            except click.exceptions.Exit:
                raise
            except Exception as error:
                message = (
                    error.format_message()
                    if isinstance(error, click.ClickException)
                    else str(error)
                )
                emit_json(self.name or "", {"error": message})
                raise click.exceptions.Exit(_error_exit(self.name)) from error


def emit_json(command: str, payload: dict[str, Any]) -> None:
    """Write one schema-versioned document to the caller's original stdout."""
    ctx = click.get_current_context(silent=True)
    stream = ctx.meta.get("json_stdout") if ctx else None
    document = {"schema_version": 1, "command": command, **payload}
    click.echo(json.dumps(document, ensure_ascii=False, allow_nan=False), file=stream)


def command_failure(message: str) -> NoReturn:
    """Preserve human failure behavior while giving JSON callers a real error."""
    ctx = click.get_current_context()
    if ctx.params.get("json_output"):
        emit_json(ctx.command.name or "", {"error": message})
        raise click.exceptions.Exit(_error_exit(ctx.command.name))
    click.echo(message, err=True)
    raise click.exceptions.Exit(1)


def status_document(graph: RevisionGraph, heads: set[str], database: str) -> dict[str, Any]:
    applied = set()
    for head in heads:
        if head in graph.migrations:
            applied.update(graph.walk_to_root(head))
    applied.intersection_update(graph.migrations)
    script_heads = set(graph.heads())
    return {
        "database": database,
        "current_heads": sorted(heads),
        "script_heads": sorted(script_heads),
        "pending": sorted(set(graph.migrations) - applied),
        "applied": sorted(applied),
        "at_head": heads == script_heads,
    }


def history_document(graph: RevisionGraph, applied: set[str] | None) -> dict[str, Any]:
    return {
        "revisions": [
            {
                "revision": migration.revision,
                "down_revisions": list(migration.down_revisions),
                "description": migration.description,
                "create_date": migration.create_date,
                "path": str(migration.path),
                "applied": migration.revision in applied if applied is not None else None,
                "irreversible": irreversible_reason(graph, migration.revision),
            }
            for migration in graph.migrations.values()
        ]
    }


def lint_document(report: LintReport) -> dict[str, Any]:
    findings = [
        {
            "rule": result.rule,
            "severity": result.severity.value,
            "blocking": result.rule in GATE_RULES and result.severity == Severity.ERROR,
            "file": result.file,
            "line": result.line,
            "message": result.message,
            "waived": result.waived,
        }
        for result in report.results
    ]
    return {
        "findings": findings,
        "counts": {
            "total": len(findings),
            "error": report.error_count,
            "warning": report.warning_count,
            "info": report.info_count,
            "blocking": sum(finding["blocking"] for finding in findings),
            "waived": sum(finding["waived"] is not None for finding in findings),
        },
    }


def diff_document(diffs: list[SchemaDiff]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "type": item.obj_type,
                "name": item.name,
                "status": item.status.value,
                "details": [
                    {
                        "field": detail.field_name,
                        "local": detail.local_value,
                        "remote": detail.remote_value,
                        "message": detail.message,
                    }
                    for detail in item.field_diffs
                ],
            }
            for item in diffs
        ],
        "in_sync": all(item.status == DiffStatus.IN_SYNC for item in diffs),
    }


def _error_exit(command: str | None) -> int:
    return 1 if command == "lint" else 2
