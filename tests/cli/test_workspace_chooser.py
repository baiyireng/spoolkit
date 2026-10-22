"""CLI 层要把"说不清是哪一个"的交互**接回来**。

背景（Task 2 的评审留了个跨任务隐患）：内核的 `resolve_root` 只在调用方显式注入
`choose` / `interactive` 时才弹选单——"内核不碰终端、交互由入口注入"是设计方向。
但 CLI 入口原先正是"什么都不传"的那种调用，于是在**登记表里有多个工作区、而当前
目录不是工作区**时，终端从"弹选单"变成"报错退 2"。这是真回归，Task 5 必须补上。

这里同时钉住两侧：人在终端前要问（并能用他选的那个），问不了的时候照老文案报错
（`tests/test_workspace_root.py` 钉着那段文案，一字不许改）。
"""

from pathlib import Path

from spoolkit import workspace
from spoolkit.cli import app as cli_app
from spoolkit.kernel import sessions
from spoolkit.kernel import workspaces as kws


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def _stand_outside_two(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """登记两个工作区，人站在一个"不是工作区"的目录里。"""
    left = _ws(tmp_path, "left")
    right = _ws(tmp_path, "right")
    kws.register(left)
    kws.register(right)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    return left, right


def test_人在终端前就弹选单并用他选的那个(tmp_path: Path, monkeypatch, capsys) -> None:
    _, right = _stand_outside_two(tmp_path, monkeypatch)
    sessions.create_session(right, title="右边那条")
    monkeypatch.setattr(workspace, "_interactive", lambda: True)
    monkeypatch.setattr(
        workspace, "_ask_which", lambda items: next(p for p in items if p.name == "right")
    )

    assert cli_app.main(["session"]) == 0

    out = capsys.readouterr().out
    assert "右边那条" in out  # 用的是他选的那个工作区


def test_问不了就照老文案报错(tmp_path: Path, monkeypatch, capsys) -> None:
    left, right = _stand_outside_two(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace, "_interactive", lambda: False)

    assert cli_app.main(["session"]) == 2

    err = capsys.readouterr().err
    assert "说不清是哪一个" in err
    assert str(left) in err and str(right) in err
    assert "用 `--root <路径>` 指定一个。" in err


def test_选工作区时CtrlC安静退出(tmp_path: Path, monkeypatch) -> None:
    """人在「选哪个」处按 Ctrl-C：退出码 130，不把 KeyboardInterrupt 摔成 traceback。"""
    _stand_outside_two(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace, "_interactive", lambda: True)

    def _cancel(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _cancel)
    assert cli_app.main(["session"]) == 130


def test_选工作区时EOF安静退出(tmp_path: Path, monkeypatch) -> None:
    _stand_outside_two(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace, "_interactive", lambda: True)

    def _eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert cli_app.main(["session"]) == 130


def test_显式给出root时不问也不报错(tmp_path: Path, monkeypatch, capsys) -> None:
    left, right = _stand_outside_two(tmp_path, monkeypatch)
    sessions.create_session(left, title="左边那条")
    monkeypatch.setattr(workspace, "_interactive", lambda: True)
    asked = []
    monkeypatch.setattr(workspace, "_ask_which", lambda items: asked.append(items) or items[0])

    assert cli_app.main(["session", "--root", str(left)]) == 0

    assert asked == []  # 显式给了就别问
    assert "左边那条" in capsys.readouterr().out
