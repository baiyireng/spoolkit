from __future__ import annotations

import io
import os

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


def test_state_style_maps_and_falls_back() -> None:
    assert term.state_style("up") == "state.up"
    assert term.state_style("down") == "state.down"
    assert term.state_style("nope") == "state.unknown"


def test_force_terminal_emits_ansi() -> None:
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
