"""Shared Rich console + semantic theme for CLI human output."""

from __future__ import annotations

from rich.console import Console
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
    if stderr:
        if _stderr is None:
            _stderr = Console(stderr=True, theme=THEME, highlight=False)
        return _stderr
    if _stdout is None:
        _stdout = Console(theme=THEME, highlight=False)
    return _stdout


def state_style(state: str) -> str:
    key = f"state.{state}"
    if key in STATE_STYLE_KEYS:
        return key
    return "state.unknown"


def print_out(renderable: object) -> None:
    console().print(renderable, markup=True, highlight=False)


def print_err(renderable: object) -> None:
    console(stderr=True).print(renderable, markup=True, highlight=False)
