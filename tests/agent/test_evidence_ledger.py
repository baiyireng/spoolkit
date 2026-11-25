"""L4 最小证据账本：没有证据的「完成」在报告里降级为「未验证」（P0）。

规矩是一句话：**凡是「完成」的声明都要挂上证据**（跑过哪条检查、结果如何）；
一条有结论的检查都挂不上时，那句完成只能算「未验证」。

**P0 的纪律**：只在报告层降级——不改 `finished`、不改落盘判定、不新增请示流。
所以这一组用例除了断言那句降级话术，还要断言结局**一个字都没变**
（`finished` 仍然是模型自己宣告的那个值）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from spoolkit.agent.audit import (
    CANNOT,
    EVIDENCE_GAPS_KIND,
    FAIL,
    PASS,
    ReviewLedger,
)
from spoolkit.agent.loop import AgentLoop
from spoolkit.cli.events import EventWriter
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, register_edit_tools
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult


# ---- 账本本身 ----------------------------------------------------------------


def test_查不了的检查不算证据() -> None:
    ledger = ReviewLedger()
    ledger.record("这次运行的完成结论", "自动验证", CANNOT, "没有挂验证器")

    assert ledger.gaps() == ["这次运行的完成结论"]
    note = ledger.downgrade_note()
    assert "未验证" in note
    assert "自动验证 → 查不了" in note


def test_有结论的检查算证据() -> None:
    for result in (PASS, FAIL):
        ledger = ReviewLedger()
        ledger.record("这次运行的完成结论", "最终验证", result, "跑过一次")

        assert ledger.gaps() == []
        assert ledger.downgrade_note() == ""


def test_没登记声明就没有缺口() -> None:
    assert ReviewLedger().gaps() == []
    assert ReviewLedger().downgrade_note() == ""


# ---- 走一次真循环 ------------------------------------------------------------


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
        "why": "把这份改好",
    }


class _Verifier:
    """一个只有结论的假验证器（真验证器的形状见 tests/agent/test_final_state_and_rename.py）。"""

    def __init__(self, ok: bool):
        self.ok = ok
        self.calls = 0

    def __call__(self, changed: list[str]) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=self.ok, content="项目测试全过", raw="项目测试全过")


def _loop(
    tmp_path: Path,
    script: list,
    *,
    pending: PendingChanges,
    writer: EventWriter,
    verify=None,
) -> AgentLoop:
    """`on_event` 接**真的 `EventWriter`**：带 `kind` 的 note 曾经被它的
    `handle` 静默吞掉（`emit(kind, **data)` 与位置参数 `kind` 重名），
    用 lambda 收集时看不出来，真实运行里事件流却是空的（见 tests/cli/test_events.py）。
    """
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path, pending))
    registry.register(read_file_spec(tmp_path, pending))
    register_edit_tools(registry, tmp_path, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4),
        verify=verify,
        pending=pending,
        on_event=writer.handle,
    )


def _run(tmp_path: Path, *, verify=None, write: bool = True):
    buffer = io.StringIO()
    pending = PendingChanges(tmp_path)
    calls = [_write("store.py", "def total():\n    return 1\n")] if write else []
    loop = _loop(
        tmp_path,
        [_turn("改实现", calls), _turn("好了", final="改好了")],
        pending=pending,
        writer=EventWriter(buffer),
        verify=verify,
    )
    result = loop.run("把 total 改成返回 1")
    return loop, result, buffer


def _notes(buffer: io.StringIO) -> list[dict]:
    out = []
    for line in buffer.getvalue().strip().splitlines():
        if not line.startswith("{"):
            continue
        item = json.loads(line)
        if item.get("type") == "note":
            out.append(item)
    return out


def test_没有验证器时有改动就降级为未验证(tmp_path: Path) -> None:
    (tmp_path / "store.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    loop, result, buffer = _run(tmp_path)

    assert "证据账本" in result.final
    assert "降级为「未验证」" in result.final
    # 事件流：探针的 `evidence_gaps` 按这个 kind 数。
    gaps = [item for item in _notes(buffer) if item.get("kind") == EVIDENCE_GAPS_KIND]
    assert gaps and gaps[0]["count"] == 1
    # trace：督导看的那一份。
    assert any("证据账本" in line for line in result.trace)
    # **P0 不改结局**：模型宣告了完成，`finished` 就还是 True。
    assert result.finished is True
    assert loop._evidence_gaps == 1


def test_验证通过时不降级(tmp_path: Path) -> None:
    (tmp_path / "store.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    verifier = _Verifier(True)
    _, result, buffer = _run(tmp_path, verify=verifier)

    assert verifier.calls >= 1
    assert "证据账本" not in result.final
    assert "最终验证：通过" in result.final
    assert not [item for item in _notes(buffer) if item.get("kind") == EVIDENCE_GAPS_KIND]


def test_一处改动都没有时不打扰(tmp_path: Path) -> None:
    """只读运行没有「完成声明」可挂：不该拖着这句话。"""
    _, result, _buffer = _run(tmp_path, write=False)

    assert "证据账本" not in result.final
    assert result.final == "改好了"
