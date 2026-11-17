"""No secret shows in an error, raw or as a URL or form encodes it."""

from urllib.parse import quote, quote_plus

from ch_migrate import secrets as secrets_module
from ch_migrate.access import server
from ch_migrate.access.server import hide_secrets

SECRET = "Probe!pw/with+symbols &more"


def test_every_encoding_of_a_given_secret_is_hidden():
    text = " | ".join(
        [SECRET, quote(SECRET), quote(SECRET, safe=""), quote_plus(SECRET), "ACCESS_DENIED"]
    )
    assert hide_secrets(text, [SECRET]) == " | ".join(["[hidden]"] * 4 + ["ACCESS_DENIED"])


def test_a_secret_read_earlier_is_hidden_without_being_passed(monkeypatch):
    monkeypatch.setattr(secrets_module, "_SEEN", {SECRET})
    url = f"http://h:8123/?param_password={quote_plus(SECRET)}&database=d"
    assert hide_secrets(f"Max retries exceeded with url: {url}") == (
        "Max retries exceeded with url: http://h:8123/?param_password=[hidden]&database=d"
    )


def test_an_empty_secret_hides_nothing():
    assert hide_secrets("abc", [""]) == "abc"


def test_read_secret_remembers_what_it_read(monkeypatch, tmp_path):
    from ch_migrate.access.settings import load_settings

    (tmp_path / "config.yaml").write_text(
        "project: {name: shop}\nenvironments:\n  dev: {host: h, database: d, migration_user: m}\n"
    )
    monkeypatch.setattr(secrets_module, "_SEEN", set())
    monkeypatch.setenv("CH_DEV_ETL_PASSWORD", SECRET)
    assert (
        server.read_secret(load_settings("dev", tmp_path / "config.yaml"), "etl_password") == SECRET
    )
    assert hide_secrets(SECRET) == "[hidden]"


def test_every_secret_get_secret_returns_is_hidden(monkeypatch):
    """The migration and dictionary passwords `up` loads too, not only the access commands'."""
    from ch_migrate.secrets import get_secret

    monkeypatch.setattr(secrets_module, "_SEEN", set())
    monkeypatch.setenv("CH_DEV_DICT_READER_PASSWORD", SECRET)
    assert get_secret("dev", "dict_reader_password", required=False) == SECRET
    assert hide_secrets(f"PASSWORD '{SECRET}'") == "PASSWORD '[hidden]'"


def test_a_secret_as_sqlalchemy_escapes_it_is_hidden():
    """SQLAlchemy doubles `%` in a statement it quotes, and the
    dictionary helper writes `:` as `\\:`, so neither form matched the raw secret."""
    secret = ":lead%pct"
    text = "[SQL: PASSWORD ':lead%%pct'] | PASSWORD '\\:lead%pct'"
    assert hide_secrets(text, [secret]) == "[SQL: PASSWORD '[hidden]'] | PASSWORD '[hidden]'"
