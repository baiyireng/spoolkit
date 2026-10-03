"""收尾报告与改名残留：两件都是「说了与事实不符的话」的缺陷。

一、**验证结论的缓存键必须是内容指纹，不是路径集合**（实测抓出来的 bug）：

    rename-cross-file 首轮，那条轨迹在中途验过一次「2 处改动、导入已经断了」
    的版本（结论：不通过）。收尾时 4 处改动都已落盘，模型自己 `run_command`
    跑测试与判据跑测试都是退出码 0——可 `set(changed) <= self._verified_paths`
    只看路径，于是那份**失败原文**被原样搬进了收尾报告。路径集合只回答
    「验的是不是同一批文件」，而同一批文件可以先是坏的、后被改好。

二、**改名残留要说出来，而且不许在有残留时宣称「已统一」**：

    rename-cross-file 首轮，四个 .py 全改对、测试全过，它却在结论里写
    「整个项目里都统一了」——README.md 与 docs/design.md 里那两处旧名
    一个字没动。
"""

import json
from pathlib import Path

from spoolkit.agent.state import TaskState
from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import (
    PendingChange,
    PendingChanges,
    register_edit_tools,
    write_file_spec,
)
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult


def _state() -> TaskState:
    return TaskState(task_id="t", goal="统一改名")


def _call(path: str, name: str = "write_file") -> ToolCall:
    return ToolCall(name, {"path": path})


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


def _write(path: str, content: str, why: str = "把这份改好") -> dict:
    return {
        "name": "write_file",
        "arguments": {"path": path, "content": content},
        "why": why,
    }


def _loop(tmp_path: Path, script: list, *, verify=None, pending=None) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path, pending))
    registry.register(read_file_spec(tmp_path, pending))
    if pending is not None:
        register_edit_tools(registry, tmp_path, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4),
        verify=verify,
        pending=pending,
    )


class _Verifier:
    """可数的假验证器：第几次跑、那一轮的内容是什么，都记得下来。"""

    def __init__(self, answers: list[tuple[str, bool]]):
        self._answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, changed: list[str]) -> ToolResult:
        self.calls.append(list(changed))
        index = min(len(self.calls) - 1, len(self._answers) - 1)
        body, ok = self._answers[index]
        return ToolResult(ok=ok, content=body)


# ---- 一、缓存键 --------------------------------------------------------------


def test_内容变了就必须重新验证(tmp_path: Path) -> None:
    verifier = _Verifier([("2 处通过、2 处没过", False), ("4 处全部通过", True)])
    pending = PendingChanges(tmp_path)
    loop = _loop(
        tmp_path, [_turn("改好了", final="改好了")], verify=verifier, pending=pending
    )
    loop.run("统一改名")

    # 第一次：内容 A（导入还是断的）→ 不通过。
    loop._run_verification(["pipeline/fetch.py"])
    assert verifier.calls == [["pipeline/fetch.py"]]
    assert loop._last_verify == ("2 处通过、2 处没过", False)

    # 收尾：内容**没有变** → 允许沿用，不再起第二次进程。
    first = loop._with_final_state("改好了", _state(), [], ["pipeline/fetch.py"])
    assert len(verifier.calls) == 1
    assert "最终验证：未通过" in first

    # 内容变了（同一个路径，正文不同）→ 必须重新验证。
    (tmp_path / "pipeline").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pipeline" / "fetch.py").write_text(
        "def load_rows():\n    pass\n", encoding="utf-8"
    )
    second = loop._with_final_state("改好了", _state(), [], ["pipeline/fetch.py"])

    assert len(verifier.calls) == 2, "内容变了却沿用了旧结论"
    assert loop._last_verify is not None and loop._last_verify[1] is True
    assert "最终验证：通过" in second
    assert "2 处没过" not in second


def test_同一份内容只验一次(tmp_path: Path) -> None:
    """内容一字未变时不许重复起进程：那条规矩没有被这次修改松掉。"""
    verifier = _Verifier([("全部通过", True)])
    loop = _loop(
        tmp_path,
        [_turn("改好了", final="改好了")],
        verify=verifier,
        pending=PendingChanges(tmp_path),
    )
    loop.run("做点什么")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    loop._run_verification(["a.py"])
    loop._with_final_state("好的", _state(), [], ["a.py"])
    loop._with_final_state("好的", _state(), [], ["a.py"])

    assert verifier.calls == [["a.py"]]


def test_内容键把路径与指纹绑在一起(tmp_path: Path) -> None:
    """键的构成要能被直接读到：路径 + 内容指纹，不是路径清单。"""
    loop = _loop(tmp_path, [], pending=PendingChanges(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")

    key = loop._content_key(["a.py", "b.py"])
    assert key is not None
    assert "a.py=" in key and "b.py=" in key
    # 顺序不同、内容相同 → 同一个键（路径集合的语义保住了）。
    assert loop._content_key(["b.py", "a.py"]) == key
    # 内容变了 → 键必须变。
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert loop._content_key(["a.py", "b.py"]) != key
    # 删掉一个 → 键也要变（「不存在」是一个必须算进去的内容事实）。
    (tmp_path / "b.py").unlink()
    assert loop._content_key(["a.py", "b.py"]) != loop._content_key(["a.py"])


# ---- 二、改名残留 ------------------------------------------------------------


OLD_DEF = "def get_rows(source):\n    return []\n"
NEW_DEF = "def load_rows(source):\n    return []\n"


def _rename_project(tmp_path: Path) -> PendingChanges:
    """一个最小改名现场：代码 + README 里都写着旧名。

    待确认清单按**提出改动时**的样子登记（old_text 是旧正文、new_text 是新
    正文）——工具层就是在那一刻读的磁盘，之后磁盘上是什么不影响台账。
    """
    (tmp_path / "pipeline").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pipeline" / "fetch.py").write_text(NEW_DEF, encoding="utf-8")
    (tmp_path / "pipeline" / "report.py").write_text(
        "from pipeline.fetch import get_rows\n\n\ndef render():\n    return get_rows('x')\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("入口是 `get_rows`。\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    pending._changes["pipeline/fetch.py"] = PendingChange(
        path="pipeline/fetch.py", old_text=OLD_DEF, new_text=NEW_DEF
    )
    return pending


def test_改名残留会被报出来(tmp_path: Path) -> None:
    pending = _rename_project(tmp_path)
    loop = _loop(tmp_path, [], pending=pending)

    text = loop._lingering_text(
        _call("pipeline/fetch.py"), ToolResult(ok=True, content="已生成修改预览")
    )
    print("TEXT", repr(text))

    assert "旧名仍有残留" in text
    assert "README.md" in text
    assert "report.py" in text
    assert "get_rows" in text
    # 改过的那一份自己不算残留。
    assert "fetch.py 第" not in text
    assert "不要说「整个项目里都统一了」" in text


def test_残留清空之后不再提这件事(tmp_path: Path) -> None:
    pending = _rename_project(tmp_path)
    (tmp_path / "pipeline" / "report.py").write_text(
        "from pipeline.fetch import load_rows\n\n\ndef render():\n    return load_rows('x')\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("入口是 `load_rows`。\n", encoding="utf-8")
    loop = _loop(tmp_path, [], pending=pending)

    text = loop._lingering_text(
        _call("pipeline/fetch.py"), ToolResult(ok=True, content="已生成修改预览")
    )

    assert text == ""


def test_不是改名时不提残留(tmp_path: Path) -> None:
    """一次普通改写不该被扣上「改名」的帽子：报错了就是噪声。"""
    (tmp_path / "a.py").write_text("def keep(a):\n    return a\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "def keep(a):\n    return a + 1\n"}
    )
    loop = _loop(tmp_path, [], pending=pending)

    text = loop._lingering_text(
        _call("a.py"), ToolResult(ok=True, content="已生成修改预览")
    )

    assert text == ""


def test_留了兼容别名就不算残留(tmp_path: Path) -> None:
    """改名后原处留一行 `旧名 = 新名` 是常见写法，**不是**残留。

    实测这条误报的代价很具体（探针第 2 次复跑）：残留报告里出现了刚改过的
    那个文件自己（`pipeline/fetch.py 第 7 行 get_rows`，那正是模型自己留的
    兼容别名），模型于是掉头去删别名，连着 4 步原地打转、19 步红。
    判据是「这个名字在新正文里以标识符形态彻底消失了没有」。
    """
    old = "def get_rows(source):\n    return []\n"
    new = "def load_rows(source):\n    return []\n\n\n# 兼容旧名\nget_rows = load_rows\n"
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "fetch.py").write_text(new, encoding="utf-8")
    (tmp_path / "README.md").write_text("入口是 `load_rows`。\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    pending._changes["pkg/fetch.py"] = PendingChange(
        path="pkg/fetch.py", old_text=old, new_text=new
    )
    loop = _loop(tmp_path, [], pending=pending)

    text = loop._lingering_text(
        _call("pkg/fetch.py"), ToolResult(ok=True, content="已生成修改预览")
    )

    assert text == ""


def test_改名残留进到模型看得见的输出里(tmp_path: Path) -> None:
    """端到端：报告要回到模型面前，而不是只在内部算了一遍。"""
    pending = _rename_project(tmp_path)
    script = [
        _turn("改抓取层", [_write("pipeline/fetch.py", NEW_DEF)]),
        _turn("改好了", final="整个项目里都统一了"),
    ]
    loop = _loop(tmp_path, script, pending=pending)

    result = loop.run("把 get_rows 改成 load_rows")

    assert any("旧名仍有残留" in line for line in result.trace)
    assert "最终文件状态" in result.final
