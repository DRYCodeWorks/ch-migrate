"""One output style for every command: steps, results, warnings and errors.

Each line starts with a fixed marker (→ step, ✓ done, ! warning, ✗ error) so the
output reads the same in a terminal, a CI log or a pipe. Rich drops colour when
output is not a terminal or NO_COLOR is set; the markers and words stay.

Text between backticks in steps, results and hints is shown as a command,
highlighted in a terminal, with the backticks removed. Errors, warnings and
details print exactly as written, because they often carry database messages
that use backticks for identifiers. Nothing is parsed as Rich markup.
"""

from __future__ import annotations

import sys
from typing import NoReturn

from rich.console import Console
from rich.text import Text

# soft_wrap: never hard-wrap at the terminal width, so paths and SQL stay greppable.
out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

_COMMAND_STYLE = "bold cyan"


def step(message: str) -> None:
    """Something is starting or in progress."""
    out.print(_marked("→ ", "cyan", message))


def success(message: str) -> None:
    """Something finished as intended."""
    out.print(_marked("✓ ", "green", message))


def detail(message: str, *, stderr: bool = False) -> None:
    """An indented line under the step, result or error above it."""
    (err if stderr else out).print(Text("  " + message, style="dim"))


def hint(message: str, *, stderr: bool = False) -> None:
    """What to do next; commands in backticks are highlighted."""
    (err if stderr else out).print(_inline(message))


def warn(message: str) -> None:
    """Worth reading, but the command carries on."""
    err.print(Text.assemble(("! ", "yellow"), (message, "yellow")))


def error(message: str) -> None:
    """The command could not do what was asked."""
    err.print(Text.assemble(("✗ ", "bold red"), message))


def fail(message: str, *hints: str) -> NoReturn:
    """Print an error, then any hints, and exit with status 1."""
    error(message)
    for line in hints:
        hint(line, stderr=True)
    sys.exit(1)


def _marked(marker: str, marker_style: str, message: str) -> Text:
    text = Text(marker, style=marker_style)
    text.append_text(_inline(message))
    return text


def _inline(message: str) -> Text:
    """Render `command` spans highlighted, everything else as plain text."""
    text = Text()
    for index, part in enumerate(message.split("`")):
        text.append(part, style=_COMMAND_STYLE if index % 2 else "")
    return text
