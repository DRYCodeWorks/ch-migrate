"""Tests for SQL file rendering, splitting and execution."""

import pytest

from ch_migrate.sql import (
    Statement,
    load_statements,
    render_placeholders,
    run_sql,
    split_statements,
)


class TestSplitStatements:
    def test_two_statements(self):
        assert sqls("CREATE TABLE a (x UInt8) ENGINE = Memory;\nDROP TABLE b;") == [
            "CREATE TABLE a (x UInt8) ENGINE = Memory",
            "DROP TABLE b",
        ]

    def test_last_statement_needs_no_semicolon(self):
        assert sqls("SELECT 1;\nSELECT 2\n") == ["SELECT 1", "SELECT 2"]

    def test_empty_and_comment_only_chunks_are_dropped(self):
        assert sqls(";;\n-- just a note\n;\n/* and this */") == []

    def test_semicolon_in_single_quoted_string(self):
        assert sqls("INSERT INTO t VALUES ('a;b');SELECT 1") == [
            "INSERT INTO t VALUES ('a;b')",
            "SELECT 1",
        ]

    @pytest.mark.parametrize(
        "literal",
        [
            r"'it\'s; fine'",
            "'it''s; fine'",
            '"col;name"',
            "`col;name`",
            "`a``;b`",
            '"a"";b"',
            r'"a\";b"',
            r"`a\`;b`",
        ],
    )
    def test_escaped_quotes_and_quoted_identifiers(self, literal):
        assert sqls(f"SELECT {literal}; SELECT 2") == [f"SELECT {literal}", "SELECT 2"]

    @pytest.mark.parametrize(
        "comment",
        ["-- a; b\n", "# a; b\n", "#! a; b\n", "/* a; b */"],
    )
    def test_semicolon_in_comment(self, comment):
        assert sqls(f"SELECT 1 {comment}+ 1; SELECT 2") == [f"SELECT 1 {comment}+ 1", "SELECT 2"]

    @pytest.mark.parametrize("heredoc", ["$$a;b$$", "$tag$a;b$tag$"])
    def test_semicolon_in_heredoc(self, heredoc):
        assert sqls(f"SELECT {heredoc}; SELECT 2") == [f"SELECT {heredoc}", "SELECT 2"]

    def test_hash_without_space_is_not_a_comment(self):
        # Only "# " and "#!" start comments in ClickHouse.
        assert sqls("#not_a_comment; SELECT 2") == ["#not_a_comment", "SELECT 2"]

    def test_unterminated_string_does_not_split(self):
        assert sqls("SELECT 'a;b") == ["SELECT 'a;b"]

    @pytest.mark.parametrize("quote", ['"', "`", "$tag$", "$$"])
    def test_unterminated_quoted_run_does_not_split(self, quote):
        text = f"SELECT {quote}a;b;SELECT 2"
        assert sqls(text) == [text]

    def test_line_numbers_point_at_the_sql(self):
        statements = split_statements("-- header\n\nSELECT 1;\n\n-- note\nSELECT\n  2;")
        assert [(s.sql, s.line) for s in statements] == [("SELECT 1", 3), ("SELECT\n  2", 6)]


class TestLeadingComments:
    def test_comment_directly_above_is_kept(self):
        [statement] = split_statements("-- waiver: reason\nDROP TABLE t")
        assert statement.comments == ("waiver: reason",)

    def test_comment_block_separated_by_blank_line_is_not_kept(self):
        [statement] = split_statements("-- file header\n\nDROP TABLE t")
        assert statement.comments == ()

    def test_comments_after_previous_statement(self):
        statements = split_statements("SELECT 1;\n-- one\n/* two */\nSELECT 2")
        assert statements[1].comments == ("one", "two")

    def test_block_comment_then_blank_line_is_not_kept(self):
        [statement] = split_statements("/* header */\n\nSELECT 1")
        assert statement.comments == ()

    def test_multiline_block_comment_has_comment_lines(self):
        [statement] = split_statements("/* first\nsecond */\nSELECT 1")
        assert statement.comments == ("first", "second")
        assert statement.line == 3

    def test_only_contiguous_comment_block_attaches(self):
        [statement] = split_statements("-- header\n \n-- reason\nSELECT 1")
        assert statement.comments == ("reason",)

    def test_line_count_includes_quoted_and_comment_newlines(self):
        statements = split_statements("SELECT 'a\n;b'; /* c\n; */\nSELECT 2")
        assert [s.line for s in statements] == [1, 4]


class TestRenderPlaceholders:
    def test_known_placeholders_are_replaced(self):
        assert render_placeholders(
            "CREATE TABLE {db}.t {on_cluster}", {"db": "x", "on_cluster": ""}
        ) == ("CREATE TABLE x.t ")

    def test_unknown_braces_are_left_alone(self):
        sql = "SELECT {id:UInt64}, '{\"a\": 1}', map('k', 1), {other}"
        assert render_placeholders(sql, {"db": "x"}) == sql

    def test_doubled_braces_are_not_format_escapes(self):
        assert render_placeholders("{{db}}", {"db": "x"}) == "{x}"

    def test_replacement_is_not_recursively_formatted(self):
        assert render_placeholders("SELECT '{value}'", {"value": "{db}", "db": "x"}) == (
            "SELECT '{db}'"
        )


class TestRunSql:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CH_DATABASE", "analytics")
        monkeypatch.delenv("CH_CLUSTER", raising=False)
        sql_dir = tmp_path / "migrations" / "sql" / "history" / "tables" / "logs"
        sql_dir.mkdir(parents=True)
        return sql_dir

    def test_keyword_arguments_override_defaults(self, project):
        (project / "x.up.sql").write_text("SELECT '{db}', '{region}'")
        [statement] = load_statements("history/tables/logs/x.up.sql", db="other", region="eu")
        assert statement == Statement(sql="SELECT 'other', 'eu'", line=1)

    @pytest.mark.parametrize("content", ["", " \n ", "-- forgot\n", "/* forgot */;"])
    def test_empty_file_is_refused(self, project, content):
        (project / "x.up.sql").write_text(content)
        with pytest.raises(ValueError, match=r"x\.up\.sql"):
            run_sql("history/tables/logs/x.up.sql")

    def test_missing_file(self, project):
        with pytest.raises(FileNotFoundError, match="missing.sql"):
            run_sql("history/tables/logs/missing.sql")

    def test_cluster_defaults_and_overrides(self, project, monkeypatch):
        monkeypatch.setenv("CH_CLUSTER", "test_cluster")
        (project / "x.up.sql").write_text("SELECT '{db}', '{cluster}', '{on_cluster}'")
        [statement] = load_statements("history/tables/logs/x.up.sql")
        assert statement.sql == ("SELECT 'analytics', 'test_cluster', 'ON CLUSTER test_cluster'")
        [statement] = load_statements(
            "history/tables/logs/x.up.sql", cluster="override", on_cluster="custom"
        )
        assert statement.sql == "SELECT 'analytics', 'override', 'custom'"


def sqls(text: str) -> list[str]:
    return [s.sql for s in split_statements(text)]
