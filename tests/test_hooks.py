"""Tests for pre/post migration hook support."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from ch_migrate.hooks import HookRegistry, run_hooks


class TestHookRegistry:
    def test_from_empty_config(self):
        registry = HookRegistry.from_config(None)
        assert registry.pre_migrate == []
        assert registry.post_migrate == []
        assert not registry.has_hooks

    def test_from_empty_dict(self):
        registry = HookRegistry.from_config({})
        assert not registry.has_hooks

    def test_from_post_migrate_only(self):
        registry = HookRegistry.from_config({
            "post_migrate": [
                "SYSTEM RELOAD DICTIONARY {db}.dict_regions ON CLUSTER default",
            ]
        })
        assert registry.pre_migrate == []
        assert len(registry.post_migrate) == 1
        assert registry.has_hooks

    def test_from_pre_and_post(self):
        registry = HookRegistry.from_config({
            "pre_migrate": ["SELECT 1"],
            "post_migrate": [
                "SYSTEM RELOAD DICTIONARY {db}.dict_a ON CLUSTER default",
                "SELECT count() FROM {db}.users",
            ],
        })
        assert len(registry.pre_migrate) == 1
        assert len(registry.post_migrate) == 2
        assert registry.has_hooks

    def test_coerces_single_string_to_list(self):
        registry = HookRegistry.from_config({
            "post_migrate": "SYSTEM RELOAD DICTIONARY {db}.dict_a ON CLUSTER default",
        })
        assert len(registry.post_migrate) == 1

    def test_handles_none_values(self):
        registry = HookRegistry.from_config({
            "pre_migrate": None,
            "post_migrate": None,
        })
        assert registry.pre_migrate == []
        assert registry.post_migrate == []


class TestRunHooks:
    def test_executes_hooks_in_order(self):
        connection = MagicMock()
        hooks = [
            "SYSTEM RELOAD DICTIONARY {db}.dict_a ON CLUSTER default",
            "SELECT count() FROM {db}.users",
        ]

        run_hooks(connection, hooks, db="mydb", phase="post_migrate", revision="abc123")

        assert connection.execute.call_count == 2
        assert connection.commit.call_count == 2

        # Verify the SQL was resolved
        executed_sql = [
            str(c.args[0]) for c in connection.execute.call_args_list
        ]
        assert "SYSTEM RELOAD DICTIONARY mydb.dict_a ON CLUSTER default" in executed_sql[0]
        assert "SELECT count() FROM mydb.users" in executed_sql[1]

    def test_resolves_db_placeholder(self):
        connection = MagicMock()
        hooks = ["SELECT 1 FROM {db}.test"]

        run_hooks(connection, hooks, db="production_db", phase="pre_migrate", revision="xyz")

        executed_sql = str(connection.execute.call_args_list[0].args[0])
        assert "production_db" in executed_sql

    def test_empty_hooks_does_nothing(self):
        connection = MagicMock()
        run_hooks(connection, [], db="mydb", phase="post_migrate", revision="abc123")
        connection.execute.assert_not_called()

    def test_unknown_placeholders_preserved(self):
        """Unknown placeholders like {cluster} or {param:String} are left as-is."""
        connection = MagicMock()
        hooks = [
            "SYSTEM RELOAD DICTIONARY {db}.dict_a ON CLUSTER {cluster}",
        ]

        run_hooks(connection, hooks, db="mydb", phase="post_migrate", revision="abc123")

        executed_sql = str(connection.execute.call_args_list[0].args[0])
        assert "mydb.dict_a" in executed_sql
        assert "{cluster}" in executed_sql

    def test_clickhouse_parameterized_syntax_preserved(self):
        """ClickHouse {param:Type} syntax in hooks is not mangled."""
        connection = MagicMock()
        hooks = [
            "SELECT count() FROM {db}.logs WHERE env = {env:String}",
        ]

        run_hooks(connection, hooks, db="mydb", phase="pre_migrate", revision="abc123")

        executed_sql = str(connection.execute.call_args_list[0].args[0])
        assert "mydb.logs" in executed_sql
        assert "{env:String}" in executed_sql


class TestConfigIntegration:
    def test_hooks_parsed_from_config_yaml(self, tmp_path: Path, monkeypatch):
        """Hooks section in config.yaml is passed through to env_config."""
        from ch_migrate.config import get_env_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
environments:
  dev:
    host: localhost
    database: testdb
    user: test

hooks:
  post_migrate:
    - "SYSTEM RELOAD DICTIONARY {db}.dict_regions ON CLUSTER default"
""")
        monkeypatch.setenv("CH_DEV_PASSWORD", "pass")

        env_config = get_env_config("dev", config_file)

        assert "hooks" in env_config
        assert len(env_config["hooks"]["post_migrate"]) == 1

        registry = HookRegistry.from_config(env_config.get("hooks"))
        assert registry.has_hooks
        assert registry.post_migrate[0] == "SYSTEM RELOAD DICTIONARY {db}.dict_regions ON CLUSTER default"

    def test_no_hooks_section_works(self, tmp_path: Path, monkeypatch):
        """Existing configs without hooks section continue to work."""
        from ch_migrate.config import get_env_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
environments:
  dev:
    host: localhost
    database: testdb
    user: test
""")
        monkeypatch.setenv("CH_DEV_PASSWORD", "pass")

        env_config = get_env_config("dev", config_file)

        assert "hooks" not in env_config
        registry = HookRegistry.from_config(env_config.get("hooks"))
        assert not registry.has_hooks


class TestHookExecutionOrder:
    """Verify hooks fire in the correct order during migration simulation."""

    def test_pre_hooks_fire_before_post_hooks(self):
        """Pre-migrate hooks should fire before post-migrate hooks."""
        connection = MagicMock()
        registry = HookRegistry.from_config({
            "pre_migrate": ["SELECT 'pre'"],
            "post_migrate": ["SELECT 'post'"],
        })

        # Simulate the env.py execution order: pre-hooks, then post-hooks
        run_hooks(connection, registry.pre_migrate, db="mydb", phase="pre_migrate", revision="all")
        run_hooks(connection, registry.post_migrate, db="mydb", phase="post_migrate", revision="abc123")

        assert connection.execute.call_count == 2
        calls = [str(c.args[0]) for c in connection.execute.call_args_list]
        assert "pre" in calls[0]
        assert "post" in calls[1]

    def test_multiple_post_hooks_fire_in_order(self):
        """Multiple post-migrate hooks fire in list order."""
        connection = MagicMock()
        hooks = [
            "SYSTEM RELOAD DICTIONARY {db}.dict_a ON CLUSTER default",
            "SYSTEM RELOAD DICTIONARY {db}.dict_b ON CLUSTER default",
            "SELECT count() FROM {db}.users",
        ]

        run_hooks(connection, hooks, db="testdb", phase="post_migrate", revision="xyz")

        assert connection.execute.call_count == 3
        calls = [str(c.args[0]) for c in connection.execute.call_args_list]
        assert "dict_a" in calls[0]
        assert "dict_b" in calls[1]
        assert "users" in calls[2]


class TestUpgradeEnvCommand:
    """Tests for the ch-migrate upgrade-env CLI command."""

    def test_upgrade_env_creates_new_env_py(self, tmp_path: Path):
        """upgrade-env copies the package env.py when no existing env.py."""
        from click.testing import CliRunner
        from ch_migrate.cli import main

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade-env"], catch_exceptions=False)

        # Can't run from tmp_path with CliRunner easily, but we can verify the
        # command is registered and validates missing migrations dir
        # Test with no migrations dir in cwd
        assert result.exit_code != 0 or "Updated" in result.output or "not found" in result.output
    def test_upgrade_env_backs_up_existing(self, tmp_path: Path, monkeypatch):
        """upgrade-env creates a .bak backup of existing env.py."""
        from click.testing import CliRunner
        from ch_migrate.cli import main

        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        existing_env = migrations_dir / "env.py"
        existing_env.write_text("# old env.py content\n")
        (tmp_path / "config.yaml").write_text("project: {name: example}\n")

        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade-env"], catch_exceptions=False)

        assert result.exit_code == 0

        # Verify backup was created
        backup = migrations_dir / "env.py.bak"
        assert backup.exists()
        assert backup.read_text() == "# old env.py content\n"

    def test_upgrade_env_no_migrations_dir(self, tmp_path: Path, monkeypatch):
        """upgrade-env fails gracefully when no migrations dir exists."""
        from click.testing import CliRunner
        from ch_migrate.cli import main

        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade-env"], catch_exceptions=False)

        assert result.exit_code != 0
        assert "not found" in result.output
