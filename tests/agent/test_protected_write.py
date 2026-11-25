"""受保护路径 / 越界写：系统侧那一声（探针侧早就会响）。

现场（35 次采样里的第三条缺陷）：`add-feature` 在 t=0 那一次**改掉了
`data/expenses.csv`**（哈希变）。探针侧抓到了——`probes.json` 的 `guards`
钉着这个文件的哈希，报的是「下限未达标：data/expenses.csv 被改过」；而系统侧
什么都没说：判据绿、收尾报告里一个字都没有。

这一层补的就是系统侧那一声，判据是机械的：

    （数据/资产类扩展名 **或** 工作区配置点名的路径）**且** 本次运行没有创建它
    → 高严重度发现，进事件流、trace 与收尾报告；**不阻断**。

三条边界各有一条用例：新建的 csv 不算越界（自己造的产出）、普通源码不算
（默认只盯数据/资产类，否则每次改代码都是高危、高危就不值钱了）、
工作区配置能加能收（`.agent/protected.json`）。
"""

from __future__ import annotations

import json
from pathlib import Path

from spoolkit.agent.audit import (
    ESCALATION_MARKER,
    HIGH,
    PROTECTED_CONFIG,
    ProtectedConfig,
    boundary_write,
    load_protected,
)
from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, register_edit_tools
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry

CSV = "data/expenses.csv"


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


def _loop(tmp_path: Path, script: list, *, pending: PendingChanges) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path, pending))
    registry.register(read_file_spec(tmp_path, pending))
    register_edit_tools(registry, tmp_path, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4),
        verify=None,
        pending=pending,
    )


def _existing_csv(tmp_path: Path, rows: str = "date,amount\n2025-03-01,10.0\n") -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / CSV).write_text(rows, encoding="utf-8")


# ---- 判据本身（不经循环，先钉住那两条）-------------------------------------


def test_数据类文件与配置点名的路径各给一条理由() -> None:
    rules = ProtectedConfig(paths=("assets/logo.bin",))
    hit = boundary_write(path=CSV, created=False, config=rules)
    assert hit is not None
    assert hit.kind == "protected_write"
    assert hit.severity == HIGH
    assert hit.escalate is True, "高严重度发现同时是升级挂点"
    assert ".csv" in hit.summary
    named = boundary_write(path="assets/logo.bin", created=False, config=rules)
    assert named is not None and "配置" in named.summary


def test_本次运行自己创建的文件不算越界() -> None:
    """自己造出来的产出不是「动了不该动的东西」。"""
    assert boundary_write(path=CSV, created=True) is None
    assert boundary_write(path=CSV, created=False) is not None


def test_普通源码默认不在闸门里() -> None:
    """默认只盯数据/资产类：**每一次改既有代码都报高危，等于没有高危**。"""
    assert boundary_write(path="expenses/store.py", created=False) is None
    assert boundary_write(path="README.md", created=False) is None


def test_harness自己写的目录永不入闸() -> None:
    for path in (".agent/limits.json", ".agent/protected.json", ".agent/index.db"):
        assert boundary_write(path=path, created=False) is None


def test_工作区配置能加能收(tmp_path: Path) -> None:
    """工作区级可配：`.agent/protected.json` 改扩展名表、点名路径、开严口径。"""
    (tmp_path / ".agent").mkdir(parents=True)
    config = tmp_path / PROTECTED_CONFIG

    # 没配置时：默认那套（.csv 在、.py 不在）。
    assert load_protected(tmp_path) == ProtectedConfig()
    assert boundary_write(path=CSV, created=False) is not None
    assert boundary_write(path="mod.py", created=False) is None

    # 收窄扩展名表 + 点名一个路径：.csv 不再受保护，mod.py 与点名的那条受保护。
    config.write_text(
        json.dumps(
            {"suffixes": [".parquet"], "paths": ["mod.py"]}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    rules = load_protected(tmp_path)
    assert boundary_write(path=CSV, created=False, config=rules) is None
    assert boundary_write(path="mod.py", created=False, config=rules) is not None

    # 严口径：凡是本次运行没创建过的一律受保护（默认关，见模块头）。
    config.write_text(
        json.dumps({"all_existing": True}, ensure_ascii=False), encoding="utf-8"
    )
    rules = load_protected(tmp_path)
    assert boundary_write(path="mod.py", created=False, config=rules) is not None
    assert boundary_write(path="mod.py", created=True, config=rules) is None

    # 写坏的配置退回默认值，不抛异常（附加的可见性不该让整次运行起不来）。
    config.write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_protected(tmp_path) == ProtectedConfig()


# ---- 端到端：改 data/*.csv → 高严重度发现 + 进收尾报告 ----------------------


def test_改数据文件报高危并进收尾报告(tmp_path: Path) -> None:
    """这一条就是现场那一次：模型顺手把账本数据改了，系统必须说出来。"""
    _existing_csv(tmp_path)
    pending = PendingChanges(tmp_path)
    loop = _loop(
        tmp_path,
        [
            _turn("加一行当月数据", [_write(CSV, "date,amount\n2025-03-01,10.0\n2025-03-02,3.0\n")]),
            _turn("好了", final="已加好 summary"),
        ],
        pending=pending,
    )

    result = loop.run("加一个 summary 子命令")

    high = loop.audit_log.high()
    assert [item.kind for item in high] == ["protected_write"]
    assert CSV in high[0].summary
    # 三条可见性：收尾报告、trace、事件流。
    assert ESCALATION_MARKER in result.final
    assert CSV in result.final
    assert any("受保护路径" in line for line in result.trace)
    notes = [line for line in result.trace if line.startswith("自动审查：")]
    assert notes and ESCALATION_MARKER in notes[0]


def test_新建的数据文件不报(tmp_path: Path) -> None:
    """任务就是「把这批数据导出来」时，新建的 csv 是产出，不是越界写。"""
    pending = PendingChanges(tmp_path)
    loop = _loop(
        tmp_path,
        [
            _turn("导出", [_write("data/report.csv", "a,b\n1,2\n")]),
            _turn("好了", final="导出了 data/report.csv"),
        ],
        pending=pending,
    )

    result = loop.run("导出一份 csv")

    assert loop.audit_log.high() == []
    assert "受保护路径" not in result.final


def test_改普通源码不报高危(tmp_path: Path) -> None:
    """判据要准：改既有代码是这个 agent 的本职，不该每次都被顶成高危。"""
    (tmp_path / "store.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    loop = _loop(
        tmp_path,
        [
            _turn("改实现", [_write("store.py", "def total():\n    return 1\n")]),
            _turn("好了", final="改好了"),
        ],
        pending=pending,
    )

    result = loop.run("把 total 改成返回 1")

    assert loop.audit_log.high() == []
    assert ESCALATION_MARKER not in result.final
