from __future__ import annotations

import io

from rich.console import Console

from spoolkit.cli import term


def test_theme_has_required_keys() -> None:
    required = {
        "state.up",
        "state.down",
        "state.slow",
        "state.unknown",
        "state.none",
        "muted",
        "accent",
        "ok",
        "bad",
    }
    assert required <= set(term.THEME.styles)
    assert term.CODE_THEME == "ansi_dark"


def test_state_style_maps_and_falls_back() -> None:
    assert term.state_style("up") == "state.up"
    assert term.state_style("down") == "state.down"
    assert term.state_style("nope") == "state.unknown"


def test_force_terminal_emits_ansi(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    term.reset_consoles()
    buf = io.StringIO()
    c = Console(file=buf, theme=term.THEME, force_terminal=True, color_system="truecolor")
    c.print(f"[{term.state_style('up')}]up[/]", highlight=False)
    assert "\x1b[" in buf.getvalue()


def test_no_color_env_strips_ansi(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    term.reset_consoles()
    buf = io.StringIO()
    # Fresh console that sees NO_COLOR (rich reads env at construct time).
    c = Console(file=buf, theme=term.THEME, force_terminal=True)
    c.print("[state.up]up[/]", highlight=False)
    assert "\x1b[" not in buf.getvalue()
    assert "up" in buf.getvalue()


def test_markup_false_preserves_brackets(capsys) -> None:
    term.reset_consoles()
    term.print_out("see [[mcp]] and [start]", markup=False)
    out = capsys.readouterr().out
    assert "[[mcp]]" in out
    assert "[start]" in out


def test_style_wraps_escaped_plain(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    term.reset_consoles()
    term.print_out("ok path [x]", markup=False, style="ok")
    out = capsys.readouterr().out
    assert "ok path [x]" in out
    assert "[ok]" not in out  # tags consumed when TTY; non-TTY still plain text


def test_print_md_force_terminal_emits_ansi(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    term.reset_consoles()
    buf = io.StringIO()
    c = Console(
        file=buf,
        theme=term.THEME,
        force_terminal=True,
        color_system="truecolor",
        soft_wrap=True,
    )
    # Drive the same helper path by temporarily swapping the shared console.
    monkeypatch.setattr(term, "console", lambda *, stderr=False: c)
    term.print_md("# Title\n\n- item\n\n`code`")
    out = buf.getvalue()
    assert "Title" in out
    assert "item" in out
    assert "code" in out
    assert "\x1b[" in out


def test_print_md_no_color_is_plain(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    term.reset_consoles()
    term.print_md("# Heading\n\nplain **bold** text")
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "Heading" in out or "plain" in out
    assert "bold" in out or "plain" in out


def test_print_md_blank_skips_or_empty_line(capsys) -> None:
    term.reset_consoles()
    term.print_md("")
    term.print_md("   \n")
    out = capsys.readouterr().out
    # Empty / whitespace-only: no body text; at most blank lines.
    assert "None" not in out
    assert "#" not in out


def test_wait_display_plain_path_writes_stderr(capsys) -> None:
    term.reset_consoles()
    display = term.WaitDisplay()
    display.update("模型生成中，已等待 15s / 上限 180s")
    display.stop()
    err = capsys.readouterr().err
    assert "模型生成中，已等待 15s" in err


def test_wait_display_status_stop_is_idempotent(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    term.reset_consoles()
    buf = io.StringIO()
    c = Console(
        file=buf,
        theme=term.THEME,
        force_terminal=True,
        color_system="truecolor",
        soft_wrap=True,
    )
    monkeypatch.setattr(term, "console", lambda *, stderr=False: c)
    display = term.WaitDisplay()
    display.update("模型生成中，已等待 15s")
    assert display._status is not None
    display.update("模型生成中，已等待 30s")
    display.stop()
    assert display._status is None
    display.stop()  # idempotent


def test_stream_holds_tty时wait不再刷新(monkeypatch) -> None:
    """Live 上屏期间心跳必须让路，否则加载符与正文轮流抢屏。"""
    monkeypatch.delenv("NO_COLOR", raising=False)
    term.reset_consoles()
    term.set_stream_holds_tty(False)
    buf = io.StringIO()
    c = Console(
        file=buf,
        theme=term.THEME,
        force_terminal=True,
        color_system="truecolor",
        soft_wrap=True,
    )
    monkeypatch.setattr(term, "console", lambda *, stderr=False: c)
    display = term.WaitDisplay()
    display.update("模型生成中，已等待 15s")
    assert display._status is not None
    term.set_stream_holds_tty(True)
    assert display._status is None
    display.update("模型生成中，已等待 30s")
    assert display._status is None
    term.set_stream_holds_tty(False)
