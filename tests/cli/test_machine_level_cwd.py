"""机器级入口不认 cwd（设计 §3.2）。

契约一句话：**CLI 认 cwd；web / bridge / MCP 不认**。它们服务整台机器，
"我碰巧站在哪个目录"不该改变行为——2026-09-27 的事故正是这个形状：
在 `C:\\Users\\32615` 起桥，它去找 `C:\\Users\\32615\\.env`，而凭据在别处；
评审又实测出 hub 把 cwd 派生的工作区钉给了桥子进程，于是同一个工作区里
hub 起的通道拿到了 `cwd/.env` 里的模型密钥，而裸 `spool bridge` 说缺凭据。

每条用例都能证伪：把 `use_cwd` 改回默认（True）或把 `--root` 拼回去，它们就变红。
"""

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spoolkit import onboarding, settings
from spoolkit.cli.app import main
from spoolkit.cli.commands import serve as serve_module
from spoolkit.cli.commands.bridge import target_resolver
from spoolkit.cli.commands.hub import bridge_child_argv, hub_command
from spoolkit.cli.commands.serve import serve_command
from spoolkit.kernel import pointers
from spoolkit.kernel.workspaces import WorkspaceError


def _ws(tmp_path: Path, name: str) -> Path:
    """一个真工作区：带 `.agent/`，而且**带 `.env`**（密钥就爱待在那儿）。"""
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("GEMINI_API_KEY=别人的密钥\n", encoding="utf-8")
    return root.resolve()


def _serve_args(root: str = "", **kwargs) -> argparse.Namespace:
    base = dict(
        root=root,
        host="127.0.0.1",
        port=8765,
        session="",
        provider="fake",
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


def _hub_args(root: str = "") -> argparse.Namespace:
    base = vars(_serve_args(root))
    base.update(
        channel="qqbot",
        bridge_proxy="",
        access="pairing",
        sandbox=False,
        appid="",
        secret="",
        token="",
        allow_user=[],
        notify_disconnect=False,
    )
    return argparse.Namespace(**base)


def _capture_serve(monkeypatch, module: str, seen: dict) -> None:
    """把 `serve` 换掉并记下调用：第一个位置参数就是 project_root。"""

    def fake(project_root, *args, **kwargs):
        seen["root"] = project_root
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(f"{module}.serve", fake)


# --- 桥：每一轮的目标不许来自 cwd ---


def test_登记表为空时桥的目标不来自cwd(tmp_path: Path, monkeypatch) -> None:
    """cwd 是个带着 `.env` 的工作区也一样：说不出工作区就**报错，不拿 cwd 凑数**。"""
    here = _ws(tmp_path, "cwd")
    monkeypatch.chdir(here)

    resolve = target_resolver(None, "bridge")

    with pytest.raises(WorkspaceError):
        resolve("chat:qqbot:u1")


def test_有指针时桥听指针不听cwd(tmp_path: Path, monkeypatch) -> None:
    """指针指着 B，而 cwd 是个带 `.env` 的工作区：**落到 B**。

    旧行为下 `find_root(cwd)` 排在指针前面，于是这条聊天在 cwd 里干活、
    模型密钥读的是 `cwd/.env`——正是评审实测到的那个形状。
    """
    here = _ws(tmp_path, "cwd")
    other = _ws(tmp_path, "other")
    pointers.set("chat:qqbot:u1", other, "s1")
    monkeypatch.chdir(here)

    target = target_resolver(None, "bridge")("chat:qqbot:u1")

    assert target.root == other
    assert target.workspace_source == "pointer"


def test_给了root的桥按root解析(tmp_path: Path) -> None:
    """`--root` 给了就是显式值：这条通道只服务那一个工作区（向后兼容）。"""
    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    pointers.set("chat:qqbot:u1", b, "s1")

    target = target_resolver(a, "bridge")("chat:qqbot:u1")

    assert target.root == a
    assert target.workspace_source == "explicit"


# --- hub：不许把 cwd 派生的工作区钉给桥子进程 ---


def test_不给root时桥子进程不带root() -> None:
    """没有工作区就**一个 `--root` 都不给**：裸 `spool bridge` = 服务整台机器。"""
    argv = bridge_child_argv(_hub_args(""), None)

    assert "bridge" in argv
    assert "--root" not in argv


def test_hub机器级不把cwd钉给子进程(tmp_path: Path, monkeypatch) -> None:
    """cwd 是个带 `.env` 的工作区也一样：机器级就是不认它。"""
    here = _ws(tmp_path, "cwd")
    monkeypatch.chdir(here)
    seen: dict = {}
    proc = MagicMock()
    proc.poll.return_value = 0  # 已经退了：收尾时不用真去杀谁

    def fake_popen(argv, **_kwargs):
        seen["argv"] = list(argv)
        return proc

    monkeypatch.setattr("spoolkit.cli.commands.hub.subprocess.Popen", fake_popen)
    _capture_serve(monkeypatch, "spoolkit.cli.commands.hub", seen)

    assert hub_command(_hub_args("")) == 0

    assert "--root" not in seen["argv"], "cwd 被当成工作区钉给了桥子进程"
    assert seen["root"] is None, "机器级网页壳不该在启动时就钉一个工作区"
    assert seen["kwargs"]["pinned"] is False
    assert seen["kwargs"]["extra_health"] is not None


def test_hub给了root仍然钉住(tmp_path: Path, monkeypatch) -> None:
    """老行为逐字保留：`hub --root X` 的网页壳与桥子进程都在 X 里。"""
    a = _ws(tmp_path, "a")
    seen: dict = {}
    proc = MagicMock()
    proc.poll.return_value = 0
    monkeypatch.setattr(
        "spoolkit.cli.commands.hub.subprocess.Popen",
        lambda argv, **_kwargs: seen.update(argv=list(argv)) or proc,
    )
    _capture_serve(monkeypatch, "spoolkit.cli.commands.hub", seen)

    assert hub_command(_hub_args(str(a))) == 0

    assert seen["argv"][seen["argv"].index("--root") + 1] == str(a)
    assert str(seen["root"]) == str(a)
    assert seen["kwargs"]["pinned"] is True
    assert seen["kwargs"]["target"].root == a


def test_hub不给root时网页壳也不认cwd(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "cwd")
    monkeypatch.chdir(here)
    seen: dict = {}
    _capture_serve(monkeypatch, "spoolkit.cli.commands.hub", seen)

    assert hub_command(_hub_args("")) == 0

    assert seen["root"] is None
    assert seen["kwargs"]["pinned"] is False


# --- serve：机器级同样不解析目标 ---


def test_serve不给root时不钉目标(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "cwd")
    monkeypatch.chdir(here)
    seen: dict = {}
    _capture_serve(monkeypatch, "spoolkit.cli.commands.serve", seen)

    assert serve_command(_serve_args("")) == 0

    assert seen["root"] is None, "机器级网页壳把 cwd 当成了工作区"
    assert seen["kwargs"]["pinned"] is False
    assert "target" not in seen["kwargs"], "没解析目标就不该编一个出来"


def test_serve给了root仍然钉住并带上来源(tmp_path: Path, monkeypatch) -> None:
    a = _ws(tmp_path, "a")
    seen: dict = {}
    _capture_serve(monkeypatch, "spoolkit.cli.commands.serve", seen)

    assert serve_command(_serve_args(str(a))) == 0

    assert str(seen["root"]) == str(a)
    assert seen["kwargs"]["pinned"] is True
    assert seen["kwargs"]["target"].workspace_source == "explicit"
    assert seen["kwargs"]["session"] == "cli"


# --- 配置向导：不许把模型密钥写进 cwd ---


def test_机器级入口的向导不拿cwd当工作区(tmp_path: Path, monkeypatch) -> None:
    """`spool serve` 没给 `--root` 时向导要**知道没有工作区**。

    旧写法 `Path(getattr(args, "root", ".")).resolve()` 在空串时就是 cwd，
    于是密钥落进"你碰巧站在哪个目录"，而向导还这么告诉人。
    """
    here = _ws(tmp_path, "cwd")
    monkeypatch.chdir(here)
    seen: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.app.onboarding.needs_setup", lambda argv=None: True
    )
    monkeypatch.setattr("spoolkit.cli.app.onboarding.interactive", lambda: True)
    monkeypatch.setattr(
        "spoolkit.cli.app.onboarding.run_wizard",
        lambda root, **kw: seen.update(root=root),
    )
    monkeypatch.setattr("spoolkit.cli.commands.serve.serve", lambda *a, **k: None)

    assert main(["serve"]) == 0

    assert seen["root"] is None, "机器级入口没有工作区，不该拿 cwd 顶数"


def test_机器级向导把密钥写进用户级env(tmp_path: Path, monkeypatch) -> None:
    """`project_root=None` → 用户级 `.env`（`settings.env_path()`），不是 cwd。"""
    here = _ws(tmp_path, "cwd")
    monkeypatch.chdir(here)
    answers = iter(["2", "KEY-123", ""])  # 2 = Gemini

    onboarding.run_wizard(
        None, ask=lambda _prompt: next(answers), say=lambda _line: None
    )

    assert "KEY-123" in settings.env_path().read_text(encoding="utf-8")
    assert "KEY-123" not in (here / ".env").read_text(encoding="utf-8")
