"""网页壳的会话也要走内核（Task 5 评审的 Important-1）。

原先 `serve` / `hub` 把 argparse 的 `--session` 默认值（`cli`）一路传给
`web/runner.py`，而 runner 又把它当**显式值**拼进子进程命令行（`--session cli`），
于是网页壳永远钉在 `cli`：指针里明明指着另一条，页面显示一条、实际跑另一条。

修法两条一起：`--session` 的默认值改成空串，`serve`/`hub` 解析出目标后
**把解析结果显式传给子进程**。
"""

import argparse
from pathlib import Path

from spoolkit.cli.commands import serve as serve_module
from spoolkit.cli.commands.serve import serve_command
from spoolkit.kernel import pointers, sessions


def _args(root: Path, **kwargs) -> argparse.Namespace:
    base = dict(
        root=str(root),
        host="127.0.0.1",
        port=8765,
        session="",
        provider="",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        allow_read=[],
        web=True,
        no_web=False,
        web_search_api="",
        web_allow=[],
        web_deny=[],
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_网页壳的会话来自指针(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="网页壳这条")
    pointers.set("web", root, made)
    captured: dict = {}
    monkeypatch.setattr(
        serve_module, "serve", lambda project_root, **kw: captured.update(root=project_root, **kw)
    )

    assert serve_command(_args(root)) == 0

    assert captured["session"] == made  # 不是 argparse 的默认值
    assert str(captured["root"]) == str(root)


def test_显式给的会话仍然赢过指针(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    pointers.set("web", root, sessions.create_session(root, title="指针那条"))
    captured: dict = {}
    monkeypatch.setattr(
        serve_module, "serve", lambda project_root, **kw: captured.update(root=project_root, **kw)
    )

    assert serve_command(_args(root, session="asked")) == 0

    assert captured["session"] == "asked"


def test_没有指针时落回_cli(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        serve_module, "serve", lambda project_root, **kw: captured.update(root=project_root, **kw)
    )

    assert serve_command(_args(root)) == 0

    assert captured["session"] == "cli"  # 老行为：网页壳一直用 cli
