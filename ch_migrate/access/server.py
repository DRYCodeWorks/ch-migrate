"""Connecting for the live `access` commands, and the server version they need.

`show` and `plan` log in as config.yaml's `admin_user` and resolve only the admin's secret.
`apply` logs in the same way, then reads the passwords of the users it sets one for, through
`read_secret`, before its first write. `check` logs in as the migration user. Every live
command connects through `connect`, so each one refuses a server older than 25.8 the same
way: 24.8 has no `CHECK GRANT` and no `READ ON <source>` (design note, 2.5). `bootstrap`
opens its clients with `open_client` and checks the version itself, legacy mode included.

Whatever stops a live command is an `AccessError`. The command prints it and exits 2,
because exit 1 means "changes pending" for `plan`.
"""

from __future__ import annotations

import io
import os
import re
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass
from typing import Any, Iterator

from ch_migrate.access.settings import ProjectSettings, ServerSettings
from ch_migrate.secrets import (  # hide_secrets is re-exported for the access commands
    SSMJsonKeyError,
    SSMSecretNotFoundError,
    get_secret,
    hide_secrets,
)
from ch_migrate.sql import clean_database_error

# The oldest ClickHouse the live access commands run on (design note, Decision 11).
MINIMUM_VERSION = (25, 8)

_VERSION = re.compile(r"^(\d+)\.(\d+)")
# The driver's wrapping around the server's own message, and the URL it adds.
_DRIVER_PREFIX = re.compile(r"^.*?DB::Exception: ")
_FOR_URL = re.compile(r"\s*\(for url [^)]*\)")
# The server's name for an error, such as `(ACCESS_DENIED)`.
_ERROR_NAME = re.compile(r"\(([A-Z][A-Z0-9_]+)\)")


class AccessError(Exception):
    """A live access command can't go on. The command prints the message and exits 2."""


@dataclass(frozen=True)
class Login:
    """A user to log in as, and the name of the secret holding its password."""

    user: str
    secret: str


def connect_admin(settings: ProjectSettings) -> Any:
    """Log in as config.yaml's `admin_user`. No other secret is read."""
    return connect(settings, Login(settings.admin_user, "admin_password"))


def connect_migration(settings: ProjectSettings) -> Any:
    """Log in as the migration user, as `access check` does. No other secret is read."""
    return connect(settings, Login(settings.migration_user, "migration_password"))


def connect(settings: ProjectSettings, login: Login) -> Any:
    """A client for `login` on the environment's server, once the server is new enough."""
    client = open_client(settings, login)
    require_supported_version(str(client.server_version))
    return client


def open_client(settings: ProjectSettings, login: Login) -> Any:
    """A client for `login`, whatever the server's version.

    `bootstrap` checks the version itself. Every other caller goes through `connect`.
    """
    import clickhouse_connect

    server = settings.server
    if not server.host:
        raise AccessError(
            f"host is required in config.yaml for environment '{settings.environment}'"
        )
    password = read_secret(settings, login.secret)
    with server_errors(), redirect_stderr(io.StringIO()):
        # clickhouse-connect prints connection failures straight to stderr.
        return clickhouse_connect.get_client(
            host=server.host,
            port=_port(server),
            username=login.user,
            password=password,
            secure=server.secure,
            interface="https" if server.secure else "http",
            connect_timeout=10,
            send_receive_timeout=60,
            # Bound parameters, such as a password `apply` sets, go in the request body. In the
            # URL, a proxy's log or a connection error could show them.
            form_encode_query_params=True,
        )


def address(settings: ProjectSettings) -> str:
    """`host:port`, as `open_client` connects to them."""
    server = settings.server
    return f"{server.host}:{_port(server)}"


def require_supported_version(version: str) -> None:
    """Refuse a server older than 25.8, naming its version."""
    parsed = _version(version)
    if parsed is None:
        raise AccessError(f"Can't read the server's version from {version!r}.")
    if parsed < MINIMUM_VERSION:
        needed = ".".join(str(part) for part in MINIMUM_VERSION)
        raise AccessError(
            f"The server runs ClickHouse {version}. "
            f"`bootstrap` and the `access` commands that connect need {needed} or later."
        )


@contextmanager
def server_errors() -> Iterator[None]:
    """Turn the driver's errors into `AccessError`, keeping the server's first line."""
    from clickhouse_connect.driver.exceptions import ClickHouseError

    try:
        yield
    except ClickHouseError as error:
        lines = str(error).strip().splitlines()
        reason = clean_database_error(lines[0]) if lines else type(error).__name__
        raise AccessError(reason) from error


def server_message(error: Exception) -> str:
    """The first line of the server's message, with its error name, from a driver error.

    The driver's wrapping, the URL it adds, which names the host, and the server's version
    are left out. Where the message runs on past its first line, as a failed connection's
    log does, the error name from its end is kept.
    """
    whole = str(error).strip()
    lines = whole.splitlines()
    text = lines[0] if lines else type(error).__name__
    text = clean_database_error(_FOR_URL.sub("", _DRIVER_PREFIX.sub("", text, count=1)))
    names = _ERROR_NAME.findall(whole)
    if names and f"({names[-1]})" not in text:
        text = f"{text.removesuffix(' Log:')} ({names[-1]})"
    return text


def read_secret(settings: ProjectSettings, name: str) -> str:
    """One secret: the SSM path at `environments.<env>.ssm.<name>`, else `CH_<ENV>_<NAME>`."""
    value = _lookup(settings, name, required=True)
    assert value is not None  # get_secret raises when a required secret is missing
    return value


def secret_is_set(settings: ProjectSettings, name: str) -> bool:
    """Whether a secret has an SSM path or a `CH_<ENV>_<NAME>` value, without reading SSM."""
    variable = f"CH_{settings.environment.upper()}_{name.upper()}"
    return bool(settings.ssm.get(name)) or bool(os.environ.get(variable))


def optional_secret(settings: ProjectSettings, name: str) -> str | None:
    """`read_secret`, or None when neither an SSM path nor `CH_<ENV>_<NAME>` is set.

    An SSM path that can't be read is still an `AccessError`.
    """
    return _lookup(settings, name, required=False)


def _port(server: ServerSettings) -> int:
    """config.yaml's port, or HTTP's default: 8443 with TLS, else 8123."""
    return server.port or (8443 if server.secure else 8123)


def _version(version: str) -> tuple[int, int] | None:
    """The major and minor version, or None when the text doesn't start with them."""
    match = _VERSION.match(version)
    return None if match is None else (int(match[1]), int(match[2]))


def _lookup(settings: ProjectSettings, name: str, required: bool) -> str | None:
    try:
        value = get_secret(
            settings.environment,
            name,
            ssm_path=settings.ssm.get(name),
            aws_region=settings.server.aws_region,
            required=required,
        )
    except (ValueError, ImportError, SSMSecretNotFoundError, SSMJsonKeyError) as error:
        raise AccessError(str(error)) from error
    if value is None:
        return None
    return str(value)
