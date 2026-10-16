"""信号身份：发现类别和 note kind 不是同一套名字。

收尾重判曾经把 note kind ``tests_weakened`` 放进要撤的名单，账本里的
``test_removed`` 对不上，旧判词就留给 T12。这里钉三件事：

- 机器要读的信号，在 ``scripts/`` / ``web/`` / ``tests/`` 里至少出现一次；
- ``test_removed`` 在终态消失之后，重判必须把它从账本里拿掉；
- ``forget_kind`` 不接受 note kind（mypy ``arg-type``）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from spoolkit.agent.audit import HIGH, AuditLog, Finding
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.review_gate import TRIGGER_NOTES
from spoolkit.agent.signals import (
    Audience,
    FindingKind,
    MECHANICAL_KINDS,
    NOTE_SIGNALS,
    NoteKind,
    SIGNALS,
    WEAKENED_KINDS,
)
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import (
    BROKEN_IMPORT_KIND,
    DELETED_REFERENCE_KIND,
    STALE_REFERENCE_KIND,
    PendingChanges,
    register_edit_tools,
)
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = Path(__file__).resolve()
_CONSUMER_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".vue", ".js", ".mjs", ".cjs"}

#: 还没接线的 machine 信号。名字必须是注册表里的值；接上之后从这里删掉。
#: 这个元组写在本文件里，扫描消费者时跳过本文件，免得名单自己把自己算成读者。
PENDING_CONSUMERS: tuple[str, ...] = ()


def _consumer_text() -> str:
    chunks: list[str] = []
    for name in ("scripts", "web", "tests"):
        root = _REPO_ROOT / name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _CONSUMER_SUFFIXES:
                continue
            if path.resolve() == _THIS_FILE:
                continue
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _quoted(name: str, text: str) -> bool:
    return f'"{name}"' in text or f"'{name}'" in text


def _machine_names() -> set[str]:
    names = {
        kind.value
        for kind, signal in NOTE_SIGNALS.items()
        if signal.audience is Audience.MACHINE
    }
    names.update(
        kind.value
        for kind, signal in SIGNALS.items()
        if signal.audience is Audience.MACHINE
    )
    return names


def test_表里列的每一条机器信号都真的有人读() -> None:
    """``audience=machine`` 的标识必须在探针、网页或测试里出现一次。

    登记了却没人读，比不登记更糟：探针会按一个永远是 0 的 kind 计数。
    """
    machine = _machine_names()
    assert set(PENDING_CONSUMERS) <= machine
    text = _consumer_text()
    already = [name for name in PENDING_CONSUMERS if _quoted(name, text)]
    assert already == [], "名单里这些已经有读者，从 PENDING_CONSUMERS 删掉：" + "、".join(
        already
    )
    missing = sorted(
        name
        for name in machine
        if name not in PENDING_CONSUMERS and not _quoted(name, text)
    )
    assert missing == [], "这些 machine 信号没有读者：" + "、".join(missing)


def test_改弱一族在机械重判名单里_note本身不在() -> None:
    assert FindingKind.TEST_REMOVED in MECHANICAL_KINDS
    assert NoteKind.TESTS_WEAKENED not in MECHANICAL_KINDS
    assert WEAKENED_KINDS <= MECHANICAL_KINDS
    produced = {
        FindingKind.STALE_REFERENCE,
        FindingKind.DELETED_REFERENCE,
        FindingKind.BROKEN_IMPORT,
    } | WEAKENED_KINDS
    assert produced == MECHANICAL_KINDS
    assert STALE_REFERENCE_KIND == FindingKind.STALE_REFERENCE
    assert DELETED_REFERENCE_KIND == FindingKind.DELETED_REFERENCE
    assert BROKEN_IMPORT_KIND == FindingKind.BROKEN_IMPORT


def test_升级发现类别覆盖每一条触发条件() -> None:
    expected = {
        FindingKind(f"escalation_{name.lower()}") for name in TRIGGER_NOTES
    }
    actual = {
        kind
        for kind, signal in SIGNALS.items()
        if signal.family.name == "ESCALATION"
    }
    assert actual == expected


def test_forget_kind按发现类别撤掉test_removed() -> None:
    log = AuditLog()
    log.add(
        Finding(
            kind=FindingKind.TEST_REMOVED,
            severity=HIGH,
            summary="测试用例 test_keep 没了",
            path="tests/test_keep.py",
            line=1,
        )
    )
    log.forget_kind(MECHANICAL_KINDS)
    assert log.findings == []


def test_forget_kind不接受note_kind(tmp_path: Path) -> None:
    """把 note kind 交进去必须是类型错误，不能靠运行时的字符串相等蒙混。"""
    snippet = (
        "from spoolkit.agent.audit import AuditLog\n"
        "from spoolkit.agent.signals import NoteKind\n"
        "def bad() -> None:\n"
        "    AuditLog().forget_kind(NoteKind.TESTS_WEAKENED)\n"
    )
    target = tmp_path / "forget_kind_note.py"
    target.write_text(snippet, encoding="utf-8")
    env = os.environ.copy()
    # 受限会话里 mypy 不在 .venv，而在 `.tmp/tools-mypy`（见交接与 gate 的前置）。
    # 正常机器上 `uv sync --extra dev` 之后这一条是空的，不影响。
    extra = _REPO_ROOT / ".tmp" / "tools-mypy"
    prefix = [str(_REPO_ROOT / "src")]
    if extra.is_dir():
        prefix.insert(0, str(extra))
    env["PYTHONPATH"] = os.pathsep.join(prefix)
    env["MYPYPATH"] = str(_REPO_ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(_REPO_ROOT / "pyproject.toml"),
            "--no-error-summary",
            "--no-pretty",
            "--show-error-codes",
            "--follow-imports=silent",
            str(target),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = proc.stdout + proc.stderr
    if "No module named mypy" in output:
        raise AssertionError("mypy 没装，这条类型断言没跑")
    assert "[arg-type]" in output, output
    assert proc.returncode != 0


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
        "why": "改这份测试",
    }


def _loop(root: Path, script: list, *, pending: PendingChanges, events: list) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(list_dir_spec(root, pending))
    registry.register(read_file_spec(root, pending))
    register_edit_tools(registry, root, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=root, context_window=8192, max_steps=6),
        verify=_always_green,
        pending=pending,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )


def _always_green(changed) -> ToolResult:
    return ToolResult(ok=True, content="测试全过（假验证器）")


def _project(root: Path) -> str:
    original = "def test_keep():\n    assert 1 == 1\n"
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_keep.py").write_text(original, encoding="utf-8")
    return original


def _weakened_notes(events: list) -> list:
    return [
        payload
        for kind, payload in events
        if kind == "note" and payload.get("kind") == "tests_weakened"
    ]


def test_中途删掉的用例在终态加回来之后账本里不再有它(tmp_path: Path) -> None:
    """中途报 ``test_removed``，终态把用例加回来，重判后不许再留着。"""
    original = _project(tmp_path)
    weakened = "def helper():\n    return 1\n"
    pending = PendingChanges(tmp_path)
    events: list = []
    loop = _loop(
        tmp_path,
        [
            _turn("先删掉用例", [_write("tests/test_keep.py", weakened)]),
            _turn("把用例加回来", [_write("tests/test_keep.py", original)]),
            _turn("交卷", final="改完了"),
        ],
        pending=pending,
        events=events,
    )

    loop.run("整理 tests/test_keep.py，别丢掉已有用例")

    assert _weakened_notes(events), "中途没报过测试被改弱，重判没有东西可撤"
    assert loop.audit_log.of_kind(FindingKind.TEST_REMOVED) == []


def test_用例没加回来时重判仍然留下test_removed(tmp_path: Path) -> None:
    """反面：终态仍删着用例，重判不是「一律忘掉」。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    events: list = []
    loop = _loop(
        tmp_path,
        [
            _turn(
                "删掉用例",
                [_write("tests/test_keep.py", "def helper():\n    return 1\n")],
            ),
            _turn("交卷", final="改完了"),
        ],
        pending=pending,
        events=events,
    )

    loop.run("整理 tests/test_keep.py")

    assert _weakened_notes(events)
    assert loop.audit_log.of_kind(FindingKind.TEST_REMOVED)
