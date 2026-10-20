"""Idempotency corpus, advice, explicit waivers, and committed baseline boundaries."""

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from ch_migrate.baseline import baseline_exemptions, normalize_baseline, record_baseline
from ch_migrate.cli import main
from ch_migrate.idempotency import classify_idempotency
from ch_migrate.lint import IdempotencyRule, LintConfig, Severity, lint_migrations
from ch_migrate.rebase import build_revision_graph

CORPUS = yaml.safe_load((Path(__file__).parent / "corpus/idempotency.yaml").read_text())


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case["sql"])
def test_reviewed_idempotency_corpus(case):
    result = classify_idempotency(case["sql"])
    assert result.status == case["expected"]
    if case["expected"] == "fix":
        assert case["suggestion"] in result.suggestion


def test_corpus_covers_required_review_surface():
    assert len(CORPUS) >= 40
    assert {case["expected"] for case in CORPUS} == {"ok", "fix", "waiver"}


@pytest.mark.parametrize(
    "case", [case for case in CORPUS if case["expected"] == "fix"], ids=lambda case: case["sql"]
)
def test_suggested_rewrite_passes_the_gate(case):
    rewritten = _apply_advice(case["sql"], classify_idempotency(case["sql"]).suggestion)
    assert classify_idempotency(rewritten).status == "ok", rewritten


@pytest.mark.parametrize(
    ("sql", "advice"),
    [
        (
            "CREATE TABLE {db}.logs_archive (id UInt64) ENGINE = Memory",
            "Write CREATE TABLE IF NOT EXISTS {db}.logs_archive so a retry skips the existing table",
        ),
        (
            "ALTER TABLE {db}.`events` ADD COLUMN region String, DROP COLUMN legacy",
            "Write ADD COLUMN IF NOT EXISTS region so a retry skips the existing column; "
            "write DROP COLUMN IF EXISTS legacy so a retry skips the missing column",
        ),
        ("ALTER TABLE t ADD x UInt64", "Write ADD COLUMN IF NOT EXISTS x"),
        (
            "CREATE MATERIALIZED VIEW {db}.mv TO {db}.target AS SELECT id FROM {db}.src",
            "Write CREATE MATERIALIZED VIEW IF NOT EXISTS {db}.mv TO {db}.target",
        ),
        (
            "RENAME TABLE {db}.logs_old TO {db}.logs",
            "Write RENAME TABLE IF EXISTS {db}.logs_old TO {db}.logs so a retry skips the missing table",
        ),
    ],
)
def test_fix_advice_quotes_the_statement(sql, advice):
    assert advice in classify_idempotency(sql).suggestion


@pytest.mark.parametrize(
    ("sql", "advice"),
    [
        (
            "INSERT INTO {db}.logs_archive SELECT id FROM {db}.logs",
            "If nothing else writes to {db}.logs_archive, put TRUNCATE TABLE {db}.logs_archive "
            "before it so a retry starts over",
        ),
        ("INSERT INTO FUNCTION s3('x') SELECT 1", "Make it skip rows that are already there"),
        (
            "ALTER TABLE t UPDATE n = n + 1, s = 'x' WHERE id = 1",
            "n is computed from its old value",
        ),
        (
            "UPDATE t SET total = price * qty, price = 0 WHERE 1",
            "total is computed from price, which it also sets",
        ),
        ("ALTER TABLE t UPDATE status = 'done' WHERE status = 'new'", "doesn't read the columns"),
        ("ALTER TABLE t DELETE WHERE id = 1", "deletes whatever still matches the WHERE"),
        ("DELETE FROM t WHERE id = 1", "deletes whatever still matches the WHERE"),
        ("EXCHANGE TABLES {db}.old AND {db}.new", "swaps {db}.old and {db}.new back"),
        ("ALTER TABLE t ATTACH PARTITION 'p' FROM src", "write REPLACE PARTITION 'p' FROM src"),
        (
            "ALTER TABLE t MOVE PARTITION 'p' TO TABLE dst",
            "moves whatever has reached the partition",
        ),
        ("ALTER TABLE t DETACH PART 'all_1_1_0'", "Use the whole PARTITION"),
    ],
)
def test_waiver_advice_says_how_to_make_a_retry_safe(sql, advice):
    check = classify_idempotency(sql)
    assert check.status == "waiver"
    assert advice in check.suggestion
    assert "-- ch-migrate: allow-non-idempotent <reason> on the line above" in check.suggestion


@pytest.mark.parametrize(
    "rewrite",
    ["ALTER TABLE t REPLACE PARTITION 'p' FROM src", "TRUNCATE TABLE {db}.logs_archive"],
)
def test_waiver_advice_rewrites_pass_the_gate(rewrite):
    assert classify_idempotency(rewrite).status == "ok"


def test_unclassified_waiver_keeps_generic_message():
    [result] = IdempotencyRule().check("ATTACH PARTITION 'p' FROM src")
    assert "allow-non-idempotent <reason> waiver" in result.message


def test_reasoned_waiver_is_visible_info():
    [result] = IdempotencyRule().check(
        "INSERT INTO t VALUES (1)",
        comments=("ch-migrate: allow-non-idempotent fixture loads once under a reviewed runbook",),
    )
    assert result.severity == Severity.INFO
    assert "fixture loads once under a reviewed runbook" in result.message


@pytest.mark.parametrize(
    "comments",
    [
        ("ch-migrate: allow-non-idempotent",),
        ("ch-migrate: allow-non-idempotent   ",),
        ("ch-migrate: allow-non-idempotent valid", "ch-migrate: allow-non-idempotent"),
    ],
)
def test_empty_waiver_reason_still_errors(comments):
    [result] = IdempotencyRule().check("INSERT INTO t VALUES (1)", comments=comments)
    assert result.severity == Severity.ERROR
    assert "waiver needs a reason" in result.message


@pytest.mark.parametrize("rule", ["idempotency", "standalone_set"])
@pytest.mark.parametrize("level", ["off", "warn", "info", "invalid", False])
def test_config_cannot_lower_gate_rules(rule, level):
    with pytest.raises(ValueError, match="in-file waiver.*gate_baseline"):
        LintConfig.from_config({"lint": {"rules": {rule: level}}})


def test_programmatic_config_cannot_disable_gate(tmp_path):
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    (versions / "a.py").write_text(
        "revision = 'a'\ndown_revision = None\ndef upgrade():\n    pass\n"
    )
    report = lint_migrations(versions, config=LintConfig(rules={"idempotency": Severity.OFF}))
    assert report.has_errors
    assert [(r.rule, r.file) for r in report.results] == [("idempotency", "config.yaml")]


def test_record_baseline_preserves_comments_and_is_idempotent(tmp_path):
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    (versions / "a.py").write_text("revision = 'a'\ndown_revision = None\n")
    (versions / "b.py").write_text("revision = 'b'\ndown_revision = 'a'\n")
    config = tmp_path / "config.yaml"
    config.write_text(
        "# project comment\nproject: {name: example} # inline project\n"
        "lint: # lint comment\n  gate_baseline: old # reviewed boundary\n"
        "  rules:\n    destructive_changes: warn # retained rule\n"
    )
    assert record_baseline(tmp_path) == ["b"]
    updated = config.read_text()
    assert yaml.safe_load(updated)["lint"]["gate_baseline"] == "b"
    for comment in (
        "# project comment",
        "# inline project",
        "# lint comment",
        "# reviewed boundary",
        "# retained rule",
    ):
        assert comment in updated
    record_baseline(tmp_path)
    assert config.read_text() == updated
    graph = build_revision_graph(versions)
    assert baseline_exemptions(graph, ("b",)) == {"a", "b"}
    with pytest.raises(ValueError, match="missing locally"):
        baseline_exemptions(graph, ("removed",))


def test_multiple_heads_are_recorded_as_list(tmp_path):
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    for revision in ("0001", "0002"):
        (versions / f"{revision}.py").write_text(f"revision = {revision!r}\ndown_revision = None\n")
    config = tmp_path / "config.yaml"
    config.write_text("# keep\nproject:\n  name: example\n")
    assert record_baseline(tmp_path) == ["0001", "0002"]
    assert yaml.safe_load(config.read_text())["lint"]["gate_baseline"] == ["0001", "0002"]
    assert "# keep" in config.read_text()


@pytest.mark.parametrize("value", [True, 1, ["a", 1], {"a": "b"}, ""])
def test_invalid_baseline_cannot_silently_exempt_history(value):
    with pytest.raises(ValueError):
        normalize_baseline(value)


def test_init_does_not_add_baseline(tmp_path):
    result = CliRunner().invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "gate_baseline" not in config.get("lint", {})


def _apply_advice(sql, suggestion):
    """Rewrite sql the way each 'Write …' in the suggestion says."""
    for written in re.findall(r"[Ww]rite (.+?) so a retry", suggestion):
        clause = "IF NOT EXISTS" if " IF NOT EXISTS " in written else "IF EXISTS"
        head, name = written.split(f" {clause} ", 1)
        words = head.split()
        if not re.search(r"(?i)\b" + r"\s+".join(words) + r"\b", sql):
            words = words[:-1]
        pattern = r"(?i)\b" + r"\s+".join(map(re.escape, words)) + r"\s+(?="
        pattern += re.escape(name.split(" TO ")[0]) + ")"
        sql, count = re.subn(pattern, f"{head} {clause} ", sql, count=1)
        assert count == 1, (sql, written)
    return sql
