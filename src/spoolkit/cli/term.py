"""Shared Rich console + semantic theme for CLI human output."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.status import Status
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

# Markdown fenced-code highlight (Pygments name). ansi_dark stays readable on
# both dark and light terminals without monokai's heavy background assumptions.
CODE_THEME = "ansi_dark"

STATE_STYLE_KEYS = frozenset(
    k for k in THEME.styles if k.startswith("state.")
)

_stdout: Console | None = None
_stderr: Console | None = None


def reset_consoles() -> None:
    global _stdout, _stderr, _stream_holds_tty
    WaitDisplay.stop_active()
    _stream_holds_tty = False
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


def print_md(text: str, *, stderr: bool = False) -> None:
    """Print model/report body as Markdown on a color TTY; plain otherwise.

    Blank / whitespace-only input prints nothing (no spurious blank-only dump).
    ``--events`` callers must not use this on the JSON stdout channel.
    """
    body = "" if text is None else str(text)
    if not body.strip():
        return
    con = console(stderr=stderr)
    if con.is_terminal and not con.no_color:
        con.print(Markdown(body, code_theme=CODE_THEME))
        return
    con.print(body, markup=False, highlight=False)


# 流式 Live 占用 TTY 时置位：心跳 Status 与 Live 同屏会互抢光标，看起来闪烁。
_stream_holds_tty = False


def set_stream_holds_tty(active: bool) -> None:
    """HumanProgress 的 Live 开/关时调用；为 True 时 WaitDisplay 停转并不再刷新。"""
    global _stream_holds_tty
    _stream_holds_tty = bool(active)
    if _stream_holds_tty:
        WaitDisplay.stop_active()


class WaitDisplay:
    """Model-wait heartbeat: in-place Status on a color TTY; plain lines otherwise.

    ``update`` may run on the wait worker thread; ``stop`` must run on the main
    thread after the worker joins (see ``WaitingGateway.chat``).

    与 assistant Live 互斥：流式上屏期间忽略心跳，避免加载符与正文轮流抢屏。
    """

    _active: WaitDisplay | None = None

    def __init__(self) -> None:
        self._status: Status | None = None

    @classmethod
    def stop_active(cls) -> None:
        current = cls._active
        if current is not None:
            current.stop()

    def update(self, line: str) -> None:
        if _stream_holds_tty:
            self.stop()
            return
        text = str(line)
        con = console(stderr=True)
        if not (con.is_terminal and not con.no_color):
            print_err(text, markup=False, style="muted")
            return
        styled = f"[muted]{escape(text)}[/]"
        if self._status is None:
            self._status = Status(styled, console=con, spinner="dots")
            self._status.start()
            WaitDisplay._active = self
        else:
            self._status.update(styled)

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
        if WaitDisplay._active is self:
            WaitDisplay._active = None


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
    "CODE_THEME",
    "THEME",
    "STATE_STYLE_KEYS",
    "WaitDisplay",
    "console",
    "escape",
    "print_err",
    "print_md",
    "print_out",
    "reset_consoles",
    "set_stream_holds_tty",
    "state_style",
]
