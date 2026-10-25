"""工具能力元数据：单一来源、派生视图、冻结快照。

这一批钉三件事：

1. **新旧等价**：改动前散在 `agent/loop.py` 与 `agent/review_gate.py` 里的那几张
   工具名清单逐条冻结在下面，派生视图必须与它们**逐条相同**——有增删就红
   （顺序无关，集合相等）。这一轮是结构性重构，行为零变化。
2. **缺声明就红**：遍历**真的装配出来的**注册表（走 `assemble_loop` 那条路，
   不是测试里另抄一份注册清单），每件已注册工具都必须声明能力；反过来，
   声明表里也不许有幽灵工具（两个历史动作名除外，见下）。
3. **常量就是派生视图本身**（`is` 同一对象）：谁把派生改回手写清单，这条立刻
   红，而不是等两份清单漂移了才发现。

另外给编排层留的三个纯函数各有单测：能不能并行 / 要不要串行 / 要不要隔离。
本轮**只留接口**，没有任何批量执行或并行调度。
"""

from pathlib import Path

import pytest

from spoolkit.agent import loop as loop_module
from spoolkit.agent import review_gate
from spoolkit.agents.runtime import ROLES
from spoolkit.config import Config
from spoolkit.fetch.client import Fetched, WebPolicy
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools import capabilities as C
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.webfetch import WebAccess

# ---- 改动前的清单（逐条冻结）------------------------------------------------
#
# 来源（改动前的逐字内容）：
#   `src/spoolkit/agent/loop.py`：EDIT_TOOLS、WRITE_TOOLS = EDIT_TOOLS + (DELETE_TOOL,)、
#       COMMIT_TOOLS = WRITE_TOOLS + (CONFIRM_TOOL,)、PROGRESS_TOOLS；
#   `src/spoolkit/agent/review_gate.py`：ACTION_TOOLS、NETWORK_TOOLS、
#       NETWORK_ENTRY_TOOLS、WRITE_TOOLS（注意：它与 loop 那个同名常量**不是**
#       同一个集合——那一档不含 delete_file，只记录、不拦）。
_旧_EDIT_TOOLS = {"replace_lines", "replace_text", "write_file"}
_旧_WRITE_TOOLS = _旧_EDIT_TOOLS | {"delete_file"}
_旧_COMMIT_TOOLS = _旧_WRITE_TOOLS | {"confirm_rewrite"}
_旧_PROGRESS_TOOLS = {"write_file", "delete_file", "replace_lines", "run_command"}
_旧_ACTION_TOOLS = {"run_command", "delete_file", "move_file", "rename_file"}
_旧_NETWORK_TOOLS = {"web_fetch", "web_search"}
_旧_NETWORK_ENTRY_TOOLS = {"research"}
_旧_GATE_WRITE_TOOLS = {"write_file", "replace_text", "replace_lines"}


def test_旧清单与派生视图逐条相同() -> None:
    """新旧等价：派生出来的集合与改动前逐条相同（增删即红）。"""
    assert set(C.EDIT_TOOLS) == _旧_EDIT_TOOLS
    assert set(C.WRITE_TOOLS) == _旧_WRITE_TOOLS
    assert set(C.COMMIT_TOOLS) == _旧_COMMIT_TOOLS
    assert set(C.PROGRESS_TOOLS) == _旧_PROGRESS_TOOLS
    assert set(C.ACTION_TOOLS) == _旧_ACTION_TOOLS
    assert set(C.NETWORK_TOOLS) == _旧_NETWORK_TOOLS
    assert set(C.NETWORK_ENTRY_TOOLS) == _旧_NETWORK_ENTRY_TOOLS


def test_两个同名写工具常量各自钉住() -> None:
    """`loop.WRITE_TOOLS`（含删除）与 `review_gate.WRITE_TOOLS`（只记录）不同名同实。

    这一个区别必须留着：`review_gate` 那一档是「产出，永远只记录、不拦」，
    多一个 delete_file 就会把「删除」从动作拦截里漏出去。
    """
    assert set(loop_module.WRITE_TOOLS) == _旧_WRITE_TOOLS
    assert set(review_gate.WRITE_TOOLS) == _旧_GATE_WRITE_TOOLS
    assert "delete_file" not in review_gate.WRITE_TOOLS


def test_请批与隔离两档也冻结() -> None:
    """新声明里的两档（本轮的判定不用它们）同样冻结，免得悄悄漂移。"""
    assert set(C.APPROVAL_TOOLS) == {
        "run_command",
        "delete_file",
        "move_file",
        "rename_file",
        "research",
        "web_fetch",
        "web_search",
    }
    assert set(C.ISOLATABLE_TOOLS) == {
        "calc",
        "dispatch",
        "research",
        "run_command",
        "web_fetch",
        "web_read",
        "web_search",
    }
    assert set(C.READ_TOOLS) == {
        "dir_stats",
        "file_symbols",
        "find_callers",
        "find_symbol",
        "list_dir",
        "read_diagnosis",
        "read_file",
        "search_code",
        "survey",
        "verify_file",
    }


def test_常量就是能力元数据里的那个视图() -> None:
    """`is` 同一对象：谁把它改回手写清单（哪怕内容一样），这条立刻红。

    这正是「不许再有手写清单」那条规矩的机械判据——两份清单各自演化才是
    漂移的来源，副本一开始相等也一样。
    """
    assert loop_module.EDIT_TOOLS is C.EDIT_TOOLS
    assert loop_module.WRITE_TOOLS is C.WRITE_TOOLS
    assert loop_module.COMMIT_TOOLS is C.COMMIT_TOOLS
    assert loop_module.PROGRESS_TOOLS is C.PROGRESS_TOOLS
    assert review_gate.ACTION_TOOLS is C.ACTION_TOOLS
    assert review_gate.NETWORK_TOOLS is C.NETWORK_TOOLS
    assert review_gate.NETWORK_ENTRY_TOOLS is C.NETWORK_ENTRY_TOOLS
    assert review_gate.WRITE_TOOLS is C.EDIT_TOOLS


# ---- 缺声明就红（遍历真装配出来的注册表）------------------------------------


def _记忆会话(root: Path):
    """挂上记忆工具要的那套东西（与 `tests/memory/test_history_tool.py` 同一条路）。"""
    from spoolkit.memory.session import MemorySession
    from spoolkit.memory.store import init_memory_schema
    from spoolkit.store.db import open_db

    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    session = MemorySession(
        conn=conn,
        hot_path=root / ".agent" / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=4096,
        session_id="new",
    )
    return conn, session


def _联网取用(root: Path) -> WebAccess:
    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        return Fetched(url=url, status=404, content_type="text/html", body=b"")

    return WebAccess(root=root, policy=WebPolicy(chunk_chars=400), fetch=fake_fetch)


def _装配出来的注册表(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """真的装配一次主循环：web、记忆、索引、编辑工具全挂上。

    项目里放一个**大文件**是有意的：小项目只注册 write_file，放个大文件才会
    把 replace_lines / replace_text 也注册进来——遍历要盖住全部工具。
    """
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "missing.toml"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "big.py").write_text("y = 1\n" * 400, encoding="utf-8")
    conn, session = _记忆会话(root)
    try:
        loop = _装配(root, session)
        return loop.registry
    finally:
        conn.close()


def _装配(root: Path, session):
    from spoolkit.cli.runtime import LoopWiring, assemble_loop

    return assemble_loop(
        root,
        FakeModel(script=[], tokenizer=OfflineTokenCounter()),
        config=Config(project_root=root, supervise=False),
        wiring=LoopWiring(
            mcp=False,
            pending=PendingChanges(root),
            memory=session,
            web=_联网取用(root),
        ),
    )


def test_每件已注册工具都声明了能力(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """遍历真注册表：缺声明就红——将来加工具忘声明，第一步就报错。"""
    registry = _装配出来的注册表(tmp_path, monkeypatch)
    names = set(registry.names())

    # 先确认这次装配真的把该有的都挂上了，否则这条用例会假绿。
    assert {
        "read_file",
        "list_dir",
        "search_code",
        "dir_stats",
        "calc",
        "web_read",
        "tool_help",
        "check_numbers",
        "request_diagnosis",
        "read_diagnosis",
        "run_command",
        "write_file",
        "replace_lines",
        "confirm_rewrite",
        "delete_file",
        "verify_file",
        "recall",
        "remember",
        "search_history",
        "dispatch",
        "research",
        "survey",
        "find_symbol",
        "file_symbols",
        "find_callers",
    } <= names

    missing = sorted(name for name in names if C.declared(name) is None)
    assert not missing, (
        f"这些已注册工具没有声明能力: {missing}——"
        "到 `spoolkit/tools/capabilities.py` 的声明表里补一行"
    )

    # `params` 必须来自工具**既有**的参数 schema，不另造一份。
    for name in sorted(names):
        spec = registry.get(name)
        assert spec is not None
        assert C.of_spec(spec).params == tuple(spec.parameters.get("properties", {})), name
        assert C.of_spec(spec).params, f"{name} 的参数名没绑上"

    # 反方向：声明表里不许有注册表里不存在的幽灵工具。
    # 四处**有意**不在主循环注册表里（都在下面点名，不许再冒新的）：
    #   - `replace_text`：实现与单测都在，但主循环按量测结论**故意不挂**它
    #     （见 `register_edit_tools` 的 docstring：给 7B 之后 6 道本来全过的题全挂）；
    #   - `web_fetch` / `web_search`：只挂进研究会话的工具表（角色工具表里查得到）；
    #   - `move_file` / `rename_file`：历史动作名，注册表里已经没有对应工具；
    #   - `orchestrate`：S10 按证据整层删掉（真机 36 跑一次没调用过），中性声明
    #     留在声明表末尾。它每位都是假、不进派生视图，删掉只会让 `of_spec` 退回
    #     `UNKNOWN`（更严不是更松）——留着是为了账目完整，不是为了拦什么。
    role_tools = {name for role in ROLES.values() for name in role.tools}
    not_registered = sorted(
        name for name in C.declared_names() if name not in names and name not in role_tools
    )
    assert not_registered == ["move_file", "orchestrate", "rename_file", "replace_text"], (
        f"声明表里有幽灵工具: {not_registered}——注册表与角色工具表里都找不到它们"
    )

    # 角色工具表（实现者 / 审查者 / 研究员）里点到的名字也必须声明过。
    missing_in_roles = sorted(name for name in role_tools if C.declared(name) is None)
    assert not missing_in_roles, f"角色工具表里有没声明能力的工具: {missing_in_roles}"


def test_结构性声明里不带schema副本() -> None:
    """`params` 由 `of_spec()` 从工具自己的 schema 绑定——声明表里不存 schema。

    这是「复用既有参数 schema，不新造一套」那条要求的机械判据：表里只有事实
    字段，参数名一律在注册表那一侧现取。
    """
    assert C.declared("write_file") is not None
    assert C.declared("write_file").params == ()
    assert C.declared("run_command").params == ()


def test_参数名从既有schema里取() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    assert C.params_of(schema) == ("a", "b")
    assert C.params_of({}) == ()
    assert C.params_of({"type": "object"}) == ()
    assert C.params_of(None) == ()  # type: ignore[arg-type]


# ---- 给编排层留的纯函数（本轮只留接口）--------------------------------------


def test_能不能并行看会不会动共享状态() -> None:
    assert C.can_parallel("read_file") is True
    assert C.can_parallel("search_code") is True
    assert C.can_parallel("web_fetch") is True
    assert C.can_parallel("write_file") is False
    assert C.can_parallel("confirm_rewrite") is False
    assert C.can_parallel("delete_file") is False
    assert C.can_parallel("run_command") is False
    # 没声明过的（外挂 MCP 工具）按最保守处理：不许并行。
    assert C.can_parallel("mcp__svc__do") is False


def test_要不要串行就是能不能并行的反面() -> None:
    for name in C.declared_names():
        assert C.requires_serial(name) is not C.can_parallel(name), name
    assert C.requires_serial("write_file") is True
    assert C.requires_serial("delete_file") is True
    assert C.requires_serial("run_command") is True
    assert C.requires_serial("read_file") is False
    assert C.requires_serial("mcp__svc__do") is True


def test_要不要隔离看外部副作用与搬不搬得动() -> None:
    # 有副本可跑的破坏性动作 / 已经有自己会话的联网动作。
    assert C.needs_isolation("run_command") is True
    assert C.needs_isolation("research") is True
    assert C.needs_isolation("web_fetch") is True
    # 删除搬不动：它要动的就是这份工作区，拦它的是批准通道而不是隔离。
    assert C.needs_isolation("delete_file") is False
    assert C.needs_isolation("read_file") is False
    assert C.needs_isolation("write_file") is False
    # 不知道的东西按最保守处理。
    assert C.needs_isolation("mcp__svc__do") is True
