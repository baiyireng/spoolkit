"""全分支评审判"合并前必修"的四条（每条一个用例，钉住行为）。

四条都是"承诺与行为不符 / 会损坏数据"，不是措辞：

1. **Task 5-M3** `spool session rename <id>` 不给标题：原先 `rc=0` 且**静默把标题清空**
   （标题是这个会话唯一的"人认得的名字"，没有历史可回滚）；
2. **Task 7-2** 同一个目录**按名字点名拿到它自己**（没有 `.agent/`，`ready=false`）、
   **按路径点名沿祖先找到真正的工作区**——用哪个写法决定落到哪份记忆/计划上；
3. **Task 7-4** `spool mcp --root` 说是"限定"，而调用方点名胜过它（落在授权边界上）；
4. **Task 4-M5** `approve_code` 会把一个从没在旧配对文件里配过对的人**写进那份
   没有通道维度的名单**，于是"在 qqbot 上批准"会扩散到 telegram。
"""

import argparse
import json
from pathlib import Path

import pytest

from spoolkit.bridge.pairing import Pairings
from spoolkit.cli.app import main
from spoolkit.kernel import pointers, sessions, trust
from spoolkit.kernel import workspaces as kws
from spoolkit.kernel.workspaces import WorkspaceError


def _ws(tmp_path: Path, name: str, mark: bool = True) -> Path:
    root = tmp_path / name
    if mark:
        (root / kws.MARK).mkdir(parents=True)
    else:
        root.mkdir(parents=True)
    return root.resolve()


# --- 1. rename 不给标题 → 报错，绝不静默清空 --------------------------------


def test_改名不给标题就报错而不是清空(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _ws(tmp_path, "ws")
    monkeypatch.chdir(root)
    made = sessions.create_session(root, title="原标题")

    assert main(["session", "rename", made]) == 2

    assert "新标题" in capsys.readouterr().err
    assert sessions.read_meta(root, made)["title"] == "原标题"  # 一个字都没动


def test_改名给了标题照旧(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "ws")
    monkeypatch.chdir(root)
    made = sessions.create_session(root, title="原标题")
    assert main(["session", "rename", made, "新标题"]) == 0
    assert sessions.read_meta(root, made)["title"] == "新标题"


# --- 2. 点名一个"登记过但还不是工作区"的目录：两种写法口径一致 -----------------


def test_不是工作区的登记项两种点名都不放行(tmp_path: Path, monkeypatch) -> None:
    real = _ws(tmp_path, "proj")
    not_yet = _ws(tmp_path, "notaws", mark=False)
    kws.register(real)
    kws.register(not_yet)
    # 站在**别的地方**敲（不然 `Path("notaws")` 恰好存在，走的是路径分支）
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(WorkspaceError) as by_name:
        kws.resolve_workspace("notaws")
    with pytest.raises(WorkspaceError):
        kws.resolve_workspace(str(not_yet))

    assert "还不是工作区" in str(by_name.value)
    assert "spool ws add" in str(by_name.value)
    # 真正的工作区照旧两种写法都行
    assert kws.resolve_workspace("proj") == real
    assert kws.resolve_workspace(str(real)) == real


def test_登记项在工作区里面时两种点名都归到那个工作区(tmp_path: Path, monkeypatch) -> None:
    """`not_yet` 放在真正的工作区**里面**：两种写法都该沿祖先归到 proj。"""
    real = _ws(tmp_path, "proj")
    inner = real / "sub"
    inner.mkdir()
    kws.register(inner)
    monkeypatch.chdir(tmp_path)

    assert kws.resolve_workspace("sub") == real
    assert kws.resolve_workspace(str(inner)) == real


# --- 3. MCP 的 --root 真的压得住点名 ----------------------------------------


def _mcp(root: Path | None):
    from spoolkit.mcp.server import McpServer

    return McpServer(root, runner=None)


def test_钉住的工作区压过点名(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)

    pinned = _mcp(a)
    answer = pinned._call_tool("delegate_task", {"goal": "干活", "workspace": "b"})
    assert answer.get("isError") is True
    assert "--root" in answer["content"][0]["text"]
    assert pinned._target({"workspace": "a"}).root == a  # 点自己那个没问题

    free = _mcp(None)  # 没给 --root：按次点名照旧
    assert free._target({"workspace": "b"}).root == b


def test_mcp点名的名字不因_cwd_里的同名目录跑偏(tmp_path: Path, monkeypatch) -> None:
    """MCP 服务是被客户端按配置拉起来的：它的 cwd 与调用方想在哪干活无关。

    实测缺陷：cwd 里有个同名的非工作区子目录时，点名会被"向上找 `.agent/`"
    劫持到外层工作区，登记表里的同名工作区永远轮不到。
    """
    target = _ws(tmp_path / "elsewhere", "snake-vue")
    parent = _ws(tmp_path, "parent")
    (parent / "snake-vue").mkdir()
    kws.register(target)
    kws.register(parent)
    monkeypatch.chdir(parent)

    assert _mcp(None)._target({"workspace": "snake-vue"}).root == target


# --- 4. 批准不再往"没有通道维度"的旧文件里写别人的放行 -------------------------


def test_批准机器级的码不改写旧文件(tmp_path: Path) -> None:
    root = _ws(tmp_path, "ws")
    legacy_path = root / ".agent" / "bridge-pairings.json"
    legacy = Pairings(legacy_path)
    legacy.ensure_code("还在等的人")  # 旧文件里有别人的码

    code = trust.ensure_code("qqbot", "openid-a", root)
    got = trust.approve_code(code)

    assert got is not None and got.user == "openid-a"
    assert trust.may_use("qqbot", "openid-a", root) is True
    # 关键：openid-a**没有**被写进那份没有通道维度的旧名单
    assert "openid-a" not in Pairings(legacy_path).approved
    # 旧文件里原本的码原样还在（没被顺手批准掉）
    assert [user for _, user in Pairings(legacy_path).pending] == ["还在等的人"]


def test_旧文件里本来有的码仍然会被同步批准(tmp_path: Path) -> None:
    """反向兼容没丢：码本来就是旧文件发的，批准它时旧文件也要认。"""
    root = _ws(tmp_path, "ws")
    legacy_path = root / ".agent" / "bridge-pairings.json"
    code = Pairings(legacy_path).ensure_code("老用户")
    trust.adopt_legacy(root, "qqbot")  # 认领：码进机器级，带上通道与工作区

    got = trust.find_code(code)
    assert got is not None and got.channel == "qqbot"
    trust.approve_code(code)

    assert Pairings(legacy_path).is_approved("老用户") is True
