"""L1.4 测试被改弱：只报不阻断（P0）。

现有防线是「按原测试判定」，它挡得住「把断言改成 `assert True`」与「删测试
文件」。挡不住的是**不在测试文件判定口径里**的那些（设计稿 §1.7 G3）：

    conftest.py / pytest 配置的 addopts / 非 .py 的测试数据 / 删用例 /
    skip·xfail / 放宽断言

这一层把它们机械地判出来，逐条给 `文件:行`。**P0 只报不阻断**：发现进事件流
（`note` / `kind=tests_weakened`）、trace 与收尾报告，不碰 `finished`、
不碰落盘判定——那两件是 P1。

零误报是硬要求：**新增**用例 / fixture / 断言不报，开关没变不报，判不了不报。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from spoolkit.agent.audit import (
    HIGH,
    TESTS_WEAKENED_KIND,
)
from spoolkit.cli.events import EventWriter
# 别名以 `_` 开头：pytest 的 `python_functions = test*` 会把导入进测试模块的
# `tests_weakened` 当成一条用例去收集（名字以 `test` 开头），而它的参数是
# 位置参数式的判据输入，收集期就会报 fixture 找不到。
from spoolkit.agent.audit import tests_weakened as _weakened
from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, register_edit_tools
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry

TESTS = (
    "def test_a():\n"
    "    assert 1 + 1 == 2\n"
    "\n"
    "\n"
    "def test_b():\n"
    "    assert 2 + 2 == 4\n"
)

CONFTEST = (
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.fixture\n"
    "def rows():\n"
    "    return [1, 2]\n"
)


def _kinds(found) -> list[str]:
    return [item.kind for item in found]


# ---- 逐条判据 ----------------------------------------------------------------


def test_删测试文件报高严重度() -> None:
    found = _weakened(path="tests/test_merge.py", before=TESTS, after="", deleted=True)

    assert _kinds(found) == ["test_deleted"]
    assert found[0].severity == HIGH
    assert found[0].path == "tests/test_merge.py"
    assert found[0].line == 1


def test_删用例报出来并给出行号() -> None:
    found = _weakened(path="tests/test_merge.py", before=TESTS, after="def test_a():\n    assert 1 + 1 == 2\n")

    assert "test_removed" in _kinds(found)
    removed = next(item for item in found if item.kind == "test_removed")
    assert "test_b" in removed.summary
    assert (removed.path, removed.line) == ("tests/test_merge.py", 5)
    assert removed.where == "tests/test_merge.py:5"


def test_加skip或xfail报出来() -> None:
    after = TESTS.replace("def test_b():", "@pytest.mark.xfail\ndef test_b():")
    found = _weakened(path="tests/test_merge.py", before=TESTS, after=after)

    assert "test_skipped" in _kinds(found)
    skipped = next(item for item in found if item.kind == "test_skipped")
    assert skipped.line == 5, "新增的那一行就是第 5 行"


def test_放宽断言少一条assert报出来() -> None:
    after = "def test_a():\n    assert 1 + 1 == 2\n\n\ndef test_b():\n    pass\n"
    found = _weakened(path="tests/test_merge.py", before=TESTS, after=after)

    assert "assert_loosened" in _kinds(found)
    loosened = next(item for item in found if item.kind == "assert_loosened")
    assert loosened.path == "tests/test_merge.py"
    assert loosened.line == 6, "改动前第 6 行那条断言没了"


def test_conftest删fixture报出来() -> None:
    found = _weakened(
        path="conftest.py", before=CONFTEST, after="import pytest\n"
    )

    assert "fixture_removed" in _kinds(found)
    removed = next(item for item in found if item.kind == "fixture_removed")
    assert removed.line == 5
    assert "rows" in removed.summary


def test_conftest新增收集期构造报出来() -> None:
    after = CONFTEST + "\ncollect_ignore = ['tests/test_slow.py']\n"
    found = _weakened(path="conftest.py", before=CONFTEST, after=after)

    assert "conftest_collection_changed" in _kinds(found)
    changed = next(item for item in found if item.kind == "conftest_collection_changed")
    assert changed.line == 8


PYPROJECT = (
    "[tool.pytest.ini_options]\n"
    'addopts = "-q"\n'
    'testpaths = ["tests"]\n'
)


def test_pytest配置新增跳过开关报出来() -> None:
    after = PYPROJECT.replace('addopts = "-q"', 'addopts = "-q -k not slow"')
    found = _weakened(path="pyproject.toml", before=PYPROJECT, after=after)

    assert "pytest_addopts_weakened" in _kinds(found)
    weakened = next(item for item in found if item.kind == "pytest_addopts_weakened")
    assert (weakened.path, weakened.line) == ("pyproject.toml", 2)
    assert "-k" in weakened.summary


def test_pytest配置收窄收集范围报出来() -> None:
    after = PYPROJECT.replace('testpaths = ["tests"]', 'testpaths = ["tests/unit"]')
    found = _weakened(path="pyproject.toml", before=PYPROJECT, after=after)

    assert "pytest_config_changed" in _kinds(found)
    changed = next(item for item in found if item.kind == "pytest_config_changed")
    assert changed.line == 3


def test_测试数据被改报出来() -> None:
    found = _weakened(
        path="tests/data/expected.csv", before="a,b\n1,2\n", after="a,b\n9,9\n"
    )

    assert _kinds(found) == ["test_data_changed"]
    assert found[0].path == "tests/data/expected.csv"
    assert "非代码" in found[0].detail


def test_新建的spec文件不算测试数据被改() -> None:
    found = _weakened(
        path="tests/unit/x.spec.ts",
        before="",
        after="it('a', () => expect(1).toBe(1))\n",
    )

    assert found == []


def test_改已有的spec文件不说成非代码golden() -> None:
    found = _weakened(
        path="tests/unit/x.spec.ts",
        before="it('a', () => expect(1).toBe(1))\n",
        after="it('a', () => expect(1).toBe(2))\n",
    )
    text = " ".join(f"{item.summary} {item.detail}" for item in found)

    assert "test_data_changed" not in _kinds(found)
    assert "非代码" not in text
    assert "golden" not in text


def test_golden_目录下的期望文件被改报出来() -> None:
    """`expected/` 是一个**路径段**时算 golden（哪怕不在 tests/ 下）。"""
    found = _weakened(
        path="expected/index.html", before="<html></html>\n", after="<html>x</html>\n"
    )

    assert _kinds(found) == ["test_data_changed"]


def test_文档名里带expected不算测试数据() -> None:
    """零误报：`docs/expected-output.md` 不是测试数据，判据要求整段相等。"""
    assert _weakened(
        path="docs/expected-output.md", before="甲\n", after="乙\n"
    ) == []


def test_测试数据被删报出来() -> None:
    found = _weakened(
        path="tests/golden/out.txt", before="x\n", after="", deleted=True
    )

    assert _kinds(found) == ["test_data_deleted"]
    assert found[0].severity == HIGH


# ---- 零误报的反例（这一组和上一条同样重要）----------------------------------


def test_新增用例与断言不报() -> None:
    after = TESTS + "\n\ndef test_c():\n    assert 3 + 3 == 6\n"
    assert _weakened(path="tests/test_merge.py", before=TESTS, after=after) == []


def test_conftest新增fixture不报() -> None:
    after = CONFTEST + "\n\n@pytest.fixture\ndef extra():\n    return 1\n"
    assert _weakened(path="conftest.py", before=CONFTEST, after=after) == []


def test_业务代码改动不报() -> None:
    assert _weakened(
        path="pipeline/report.py",
        before="def render():\n    return 1\n",
        after="def render():\n    return 2\n",
    ) == []


def test_开关没变或去掉开关都不报() -> None:
    same = _weakened(path="pyproject.toml", before=PYPROJECT, after=PYPROJECT)
    assert same == []
    dropped = _weakened(
        path="pyproject.toml",
        before=PYPROJECT.replace('addopts = "-q"', 'addopts = "-q -k slow"'),
        after=PYPROJECT,
    )
    assert dropped == []


def test_配置读不出来时不报() -> None:
    """判不了不报：一份写坏的 pyproject 不该被报成「测试被改弱」。"""
    assert _weakened(
        path="pyproject.toml", before=PYPROJECT, after="[tool.pytest\n这不是 TOML"
    ) == []


def test_相同内容不报() -> None:
    assert _weakened(path="tests/test_merge.py", before=TESTS, after=TESTS) == []


# ---- 端到端：走一次真循环，发现进账本 / 事件流 / 收尾报告 --------------------


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
        "why": "把测试改一下",
    }


def _loop(tmp_path: Path, script: list, *, pending: PendingChanges, writer):
    """`on_event` 接**真的 `EventWriter`**（不是 lambda）。

    这条是必须的：`EventWriter.handle` 曾经把带 `kind` 字段的 note 全吞掉
    （`emit(kind, **data)` 与位置参数 `kind` 重名 → TypeError → 被
    `AgentLoop._emit` 静默吞）。用 lambda 收集时它看不出来——真实运行里
    事件流却是空的。端到端必须走同一个出口。
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
        pending=pending,
        on_event=writer.handle,
    )


def test_改弱测试一次真循环里三条出口都看得见(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_merge.py").write_text(TESTS, encoding="utf-8")
    weakened = "def test_a():\n    assert 1 + 1 == 2\n"
    pending = PendingChanges(tmp_path)
    buffer = io.StringIO()
    writer = EventWriter(buffer)
    loop = _loop(
        tmp_path,
        [
            _turn("把慢的那条删掉", [_write("tests/test_merge.py", weakened)]),
            _turn("好了", final="测试都过了"),
        ],
        pending=pending,
        writer=writer,
    )

    result = loop.run("让 tests 全过")

    high = loop.audit_log.high()
    assert [item.kind for item in high] == ["test_removed"]
    assert high[0].where == "tests/test_merge.py:5"
    # ① 事件流：note 的 kind 单独成一类，探针据此数 `tests_weakened`。
    notes = [
        json.loads(line)
        for line in buffer.getvalue().strip().splitlines()
        if line.startswith("{")
    ]
    notes = [item for item in notes if item.get("type") == "note"]
    assert any(item.get("kind") == TESTS_WEAKENED_KIND for item in notes)
    # ② trace：督导看的那一份。
    assert any("自动审查" in line for line in result.trace)
    # ③ 收尾报告：人真的会读的那一份，带 file:line。
    assert "tests/test_merge.py:5" in result.final
    # ---- P1 把这条结局**有根据地改了**（这是本轮唯一一处改动 P0 纪律的断言）----
    #
    # P0 的纪律是「只报不阻断」，所以那时这里断言 `finished is True`。P1 接的
    # 正是「不许静默放行」（设计稿 §3.1 第 1 条：**L1 有任一高严重度发现** →
    # 不许宣称完成），所以删掉一条用例、还在结论里说「测试都过了」时必须
    # `finished=False`。**这仍然不是「成果不许落盘」**：改动照旧在盘上、
    # 回退命令照旧给得出（下面两条就是那两件事的断言）。
    assert result.finished is False
    assert "未完成（被审查拦下）" in result.final
    assert "spool revert --run" in result.final
