"""Shared Rich console + semantic theme for CLI human output."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.theme import Theme

THEME = Theme(
    {
        "state.up": "green",
        "state.down": "red",
        "state.slow": "yellow",
        "state.unknown": "dim",
        "state.none": "dim",
        "muted": "dim",
        "accent": "cyan",
        "ok": "green",
        "bad": "red",
    }
)

STATE_STYLE_KEYS = frozenset(
    k for k in THEME.styles if k.startswith("state.")
)

_stdout: Console | None = None
_stderr: Console | None = None


def reset_consoles() -> None:
    global _stdout, _stderr
    _stdout = None
    _stderr = None


def console(*, stderr: bool = False) -> Console:
    global _stdout, _stderr
    # soft_wrap: do not reflow long paths/notes to terminal width — callers
    # (and status cards) already wrap deliberately; Rich hard-wrap breaks
    # single-line contracts (config notes, channel effective lines, tests).
    if stderr:
        if _stderr is None:
            _stderr = Console(
                stderr=True, theme=THEME, highlight=False, soft_wrap=True
            )
        return _stderr
    if _stdout is None:
        _stdout = Console(theme=THEME, highlight=False, soft_wrap=True)
    return _stdout


def state_style(state: str) -> str:
    key = f"state.{state}"
    if key in STATE_STYLE_KEYS:
        return key
    return "state.unknown"


def print_out(
    renderable: object = "",
    *,
    markup: bool = True,
    style: str | None = None,
) -> None:
    """Print to stdout via the shared Console.

    ``markup=True`` (default): Rich markup is interpreted — callers must
    ``escape`` user/path fragments. ``markup=False``: treat as plain text
    (auto-escaped). ``style`` wraps the whole line in a theme key when set
    (plain text is escaped first).
    """
    _emit(console(), renderable, markup=markup, style=style)


def print_err(
    renderable: object = "",
    *,
    markup: bool = True,
    style: str | None = None,
) -> None:
    """Print to stderr via the shared Console (same rules as ``print_out``)."""
    _emit(console(stderr=True), renderable, markup=markup, style=style)


def _emit(
    con: Console,
    renderable: object,
    *,
    markup: bool,
    style: str | None,
) -> None:
    if style is not None:
        body = escape(str(renderable))
        con.print(f"[{style}]{body}[/]", markup=True, highlight=False)
        return
    if not markup:
        con.print(escape(str(renderable)), markup=True, highlight=False)
        return
    con.print(renderable, markup=True, highlight=False)


__all__ = [
    "THEME",
    "STATE_STYLE_KEYS",
    "console",
    "escape",
    "print_err",
    "print_out",
    "reset_consoles",
    "state_style",
]
