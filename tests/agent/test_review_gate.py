"""风险分级放行的**接线**用例（§3.2.2 / §3.2.4 / §2.5）。

这一批钉的三件事各自都有一次真现场：

- **T6 的分流**：`boundary_action()` 原先写好了**一个调用方都没有**，
  于是「关联目录（`spool extra`）读放开、**写要确认**」这条语义落不了地：
  落到关联目录里的写在无人值守时被静默丢掉，而工作区外的写一律硬拒。
  裁决是按**路径落在哪儿**分流：关联目录内 → 拦下这次写、请示一次；
  域外 → 硬拒绝 + 说清「怎么才能做」与「为什么不问」。
- **T7 的充分条件**：规模越线本身不是风险。跨文件改名天生要改 5–8 个文件
  （rename 那条探针恰好 6 个），拿「改了 6 个文件」当打扰人的理由，升级率
  就是这么被顶上去的（§3.2.4：超标回去改触发条件，**不是**调高阈值）。
- **T12 的「仍在」**：账本原先只累加不清，中途那一版的判词在模型把文件改好
  之后还挂着，收尾就拿一条**已经不成立**的判词去拦完成声明。

用例都**走真循环**（`AgentLoop.run`）或真判据函数，不 mock 判据本身。
"""

from __future__ import annotations

import json
from pathlib import Path

from spoolkit.agent.audit import ESCALATION_MARKER, HIGH, Finding
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.review_gate import (
    EscalationGate,
    outside_write_refusal,
)
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, register_edit_tools
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _write(path: str, content: str) -> dict:
    return {
        "name": "write_file",
        "arguments": {"path": path, "content": content},
        "why": "写这一份",
    }


def _loop(
    root: Path,
    script: list,
    *,
    pending: PendingChanges,
    extra_roots: tuple = (),
    events: list | None = None,
    verify=None,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(list_dir_spec(root, pending))
    registry.register(read_file_spec(root, pending))
    register_edit_tools(registry, root, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=root, context_window=8192, max_steps=6),
        verify=verify,
        pending=pending,
        read_roots=extra_roots,
        on_event=(lambda kind, payload: events.append((kind, payload)))
        if events is not None
        else None,
    )


def _always_green(changed) -> ToolResult:
    """一个「每次都验过」的假验证器：它只做一件事——让**中途那几次**
    机械审查真的跑起来（那些判词正是收尾重判要作废的东西）。"""
    return ToolResult(ok=True, content="测试全过（假验证器）")


def _escalations(events: list) -> list[dict]:
    return [
        payload
        for kind, payload in events
        if kind == "note" and payload.get("kind") == "escalate"
    ]


def _tool_details(events: list) -> str:
    return "\n".join(
        str(payload.get("detail") or "")
        for kind, payload in events
        if kind == "tool" and not payload.get("ok")
    )


# ---- T6 的第一条路：落在**已授权关联目录**里的写 ----------------------------


def test_关联目录里的写被拦下请示一次且未批准就不执行(tmp_path: Path) -> None:
    """`spool extra` 的语义是「读放开，**写要确认**」——今天却直接失败。

    这一条把那句话钉成可证伪的形状：拦下这次写、请示一次（五件齐全）、
    没批准就不执行它；而这一轮**其余成果照落盘**（§2.5 的不变量）。
    """
    root = tmp_path / "ws"
    shared = tmp_path / "shared"
    root.mkdir()
    shared.mkdir()
    pending = PendingChanges(root, extra_roots=(shared,))
    events: list = []
    target = shared / "外部的.txt"
    loop = _loop(
        root,
        [
            _turn("先在工作区里落一处产出", [_write("ok.py", "X = 1\n")]),
            _turn("再写关联目录那一份", [_write(str(target), "外部\n")]),
            _turn("交卷", final="工作区那份写好了"),
        ],
        pending=pending,
        extra_roots=(shared,),
        events=events,
    )

    result = loop.run("改一处工作区文件，另外写一份关联目录里的")

    # ① 升级了一次，触发条件是 T6，五件齐全（缺任一即无效请示，§3.2.3）。
    escalations = [item for item in _escalations(events) if item["trigger"] == "T6"]
    assert escalations, "关联目录里的写没有进闸门（T6 没接线）"
    assert escalations[0]["summary_complete"] is True
    assert escalations[0]["before_action"] is True, "拦得比动作晚"
    assert "T6" in {item.trigger for item in loop.escalation_gate.all_actions}
    # 没人批复（无人值守）→ 这条动作**挂起待批**，不是静默放行。
    assert escalations[0]["blocked"] is True
    assert any(
        item.fingerprint == escalations[0]["fingerprint"]
        for item in loop.escalation_gate.pending_actions
    ), "被拦的动作没有挂起为待批"

    # ② **动作没执行**：目标文件不在盘上，也没进待确认清单。
    assert not target.exists(), "没批准却把它写出来了"
    assert [item.path for item in pending.items()] == ["ok.py"]

    # ③ **成果照落**（§2.5 的硬不变量）：工作区那份落了盘，回退命令给得出。
    applied = pending.apply()
    assert (root / "ok.py").read_text(encoding="utf-8") == "X = 1\n"
    assert applied == ["ok.py"]
    assert "spool revert --run" in result.final, "收尾报告里没有可执行的整轮回退命令"


# ---- T6 的第二条路：工作区与所有关联目录**之外** ---------------------------


def test_域外的写直接拒绝且给出怎么才能做与为什么不问(tmp_path: Path) -> None:
    """域外保持硬拒绝，但话术要能回答两个问题：**怎么才能做** / **为什么不问**。

    这种路径没有可批准的正当情形（既不是工作区，也没人授过权），请示只会
    白花一次打扰预算——所以连问都不问，直接拒绝。
    """
    root = tmp_path / "ws"
    outside = tmp_path / "别人的目录"
    root.mkdir()
    outside.mkdir()
    target = outside / "x.txt"
    pending = PendingChanges(root)
    events: list = []
    loop = _loop(
        root,
        [
            _turn("往域外写一份", [_write(str(target), "越界\n")]),
            _turn("改回工作区里做", [_write("ok.py", "Y = 1\n")]),
            _turn("交卷", final="工作区那份写好了"),
        ],
        pending=pending,
        events=events,
    )

    result = loop.run("先写域外，再写工作区")

    # ① 拒绝发生了，而且是**工具层当场拒绝**（不是排队等批复）。
    detail = _tool_details(events)
    assert "既不在工作区里" in detail, detail
    assert not target.exists()
    assert [item.path for item in pending.items()] == ["ok.py"]
    # 域外那条**不计进升级**：没有可批准的情形，请示只是白花预算。
    assert [
        item for item in loop.escalation_gate.all_actions if item.trigger == "T6"
    ] == []

    # ② 话术的两个必答项与 §2.5 的反向条款（模型可见文本必须带，且不得读成
    #    「禁止删除」——这是设计稿 §2.5 的硬规矩）。
    text = outside_write_refusal(str(target.resolve()))
    assert "spool extra add" in text, "没告诉它怎么才能做"
    assert "没有任何可批准的正当情形" in text, "没说为什么不问"
    assert "不是「不许删文件、不许精简」" in text
    assert "§2.5 的反向条款" in text

    # ③ 成果照落：域外那一处被拒不影响工作区这一处。
    pending.apply()
    assert (root / "ok.py").is_file()
    assert "spool revert --run" in result.final


def test_没授权时读不到的话术也点出关联目录(tmp_path: Path) -> None:
    """读那一路的拒绝话术同样要给「怎么才能做」（两个入口都点出来）。"""
    root = tmp_path / "ws"
    outside = tmp_path / "别人的目录"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("x\n", encoding="utf-8")
    result = read_file_spec(root).handler({"path": str(outside / "secret.txt")})
    assert result.ok is False
    assert "spool extra add" in result.content
    assert "--allow-read" in result.content


# ---- T7：规模是必要条件，不是充分条件 --------------------------------------


def _gate(tmp_path: Path) -> EscalationGate:
    return EscalationGate(tmp_path)


def _high() -> Finding:
    return Finding(
        kind="stale_reference",
        severity=HIGH,
        summary="改名后旧名仍有残留",
        detail="old @ a.py:3",
        escalate=True,
        path="a.py",
        line=3,
    )


def _triggers(requests) -> set[str]:
    return {item.trigger for item in requests}


def test_T7_规模越线但已验证且无高危发现就不升级(tmp_path: Path) -> None:
    """「改了 6 个文件」本身不是风险：验过、没高危发现的一批不该打扰人。"""
    files = [f"pkg/m{i}.py" for i in range(6)]
    requests = _gate(tmp_path).completion_triggers(
        [], files=files, lines=30, verified=True
    )
    assert "T7" not in _triggers(requests), "规模还是被当成了充分条件"


def test_T7_规模越线加未验证或加高危发现仍升级(tmp_path: Path) -> None:
    """规模回到它该有的位置：**必要条件**——配上风险才升级。"""
    files = [f"pkg/m{i}.py" for i in range(6)]
    unverified = _gate(tmp_path).completion_triggers(
        [], files=files, lines=30, verified=False
    )
    assert "T7" in _triggers(unverified)

    risky = _gate(tmp_path).completion_triggers(
        [_high()], files=files, lines=30, verified=True
    )
    assert "T7" in _triggers(risky)


def test_T7_没越规模时再多风险也不升级(tmp_path: Path) -> None:
    """必要条件那一半没有松：改动规模不到线时 T7 不该出现。"""
    requests = _gate(tmp_path).completion_triggers(
        [_high()], files=["a.py"], lines=3, verified=False
    )
    assert "T7" not in _triggers(requests)
    # 同批的高危发现照样走 T12——T7 的细化没有削弱真高危。
    assert "T12" in _triggers(requests)


def test_T1真高危不因T7细化而消失(tmp_path: Path) -> None:
    """`T1`（删/改公共导出）是另一条判据，细化 T7 不许动它。"""
    gate = _gate(tmp_path)
    (tmp_path / "mod.py").write_text(
        "def keep():\n    return 1\n\n\ndef gone():\n    return 2\n", encoding="utf-8"
    )
    gate.snapshot_symbols("mod.py", (tmp_path / "mod.py").read_text(encoding="utf-8"))
    requests = gate.completion_triggers(
        [],
        files=["mod.py"],
        lines=2,
        written={"mod.py": "def keep():\n    return 1\n"},
        verified=True,
    )
    assert "T1" in _triggers(requests)
    assert "T7" not in _triggers(requests)


# ---- T12：判据是「仍在」，不是「曾经在」 -----------------------------------


def _rename_script(second_step: dict | None) -> list:
    script = [
        _turn(
            "改定义",
            [_write("a.py", "def load_rows():\n    return 1\n")],
        )
    ]
    if second_step is not None:
        script.append(second_step)
    script.append(_turn("交卷", final="改完了"))
    return script


def _rename_project(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def old_rows():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "b.py").write_text(
        "from a import old_rows\n\n\ndef use():\n    return old_rows()\n",
        encoding="utf-8",
    )
    (tmp_path / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")


def test_残留被修掉之后不再判高危也不再升级(tmp_path: Path) -> None:
    """模型把残留改好了，收尾就不该再拿那条旧判词拦完成声明。

    `T12` 的判据原话是「自动修 N 轮后**仍在**」——账本只累加不清时，它判的
    其实是「曾经在」，那是 §4 点名不许有的 L1 误报。现场就是 rename 那条
    探针：中途那一版 README/docs 还写着旧名，模型随后把两处都改了，收尾
    却仍报「旧名仍有残留」并按 T12 升级。
    """
    _rename_project(tmp_path)
    pending = PendingChanges(tmp_path)
    loop = _loop(
        tmp_path,
        _rename_script(
            _turn(
                "把引用一起改掉",
                [_write("b.py", "from a import load_rows\n\n\ndef use():\n    return load_rows()\n")],
            )
        ),
        pending=pending,
        verify=_always_green,
    )

    result = loop.run("把 a.py 里的 old_rows 改名成 load_rows，整个项目统一")

    assert loop.audit_log.high() == [], "旧判词还挂在账本里"
    assert "T12" not in {item.trigger for item in loop.escalation_gate.all_actions}
    assert ESCALATION_MARKER not in result.final


def test_残留没被修掉时照旧判高危并升级(tmp_path: Path) -> None:
    """反面：真没修掉的残留必须照旧拦下——重判不是「不再判」。"""
    _rename_project(tmp_path)
    pending = PendingChanges(tmp_path)
    loop = _loop(
        tmp_path, _rename_script(None), pending=pending, verify=_always_green
    )

    loop.run("把 a.py 里的 old_rows 改名成 load_rows")

    high = loop.audit_log.high()
    assert high, "残留没被修掉却没报高危"
    assert any(item.path == "b.py" for item in high)
    triggers = {item.trigger for item in loop.escalation_gate.all_actions}
    assert "T12" in triggers, "真没修掉的高危发现没有升级"
