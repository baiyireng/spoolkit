"""接线：暂存的重写走整条主循环（不是只测工具）。

单测（`tests/tools/test_edit.py`）钉的是确认入口自己的形状；这里钉的是**循环**
把这件事接对了没有——三件事单看工具都看不出来：

1. 暂存的那一步**不算产出**，所以它不该触发自动验证（否则验证只能报一句
   「没有需要验证的改动」，那是一句假通过）；
2. 确认成功、改动真的进了待确认清单之后，**必须**触发自动验证；
3. 同一份正文原样重提会被硬拦，而拦下的话术里带着能照抄的 confirm 调用——
   这是「不许重发正文」在循环这一层的落点（工具那一层拦不住重发：重发的参数
   与成功那次一字不差，正好被这条抓住）。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, register_edit_tools, write_file_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult

BLOCK = """<html>
<script>
const canvas = 1;
function draw() { return canvas; }
let score = 0;
function restart() { score = 0; }
canvas.addEventListener('keydown', restart);
</script>
<body id="board" onclick="restart()">
<style>
#board { color: red; }
.game { color: blue; }
</style>
</html>
"""

TWO = BLOCK * 3
MERGED = "<html>\n<script>\nconst canvas = 1;\n</script>\n</html>\n"
REASON = "两份文档首尾相接，合并成单文档并去掉重复声明"


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


def _write(reason: str | None = REASON) -> dict:
    args = {"path": "snake.html", "content": MERGED}
    if reason:
        args["rewrite_reason"] = reason
    return {"name": "write_file", "arguments": args}


def _read() -> dict:
    return {"name": "read_file", "arguments": {"path": "snake.html"}}


def _build(
    tmp_path: Path, script: list, pending: PendingChanges | None = None
) -> tuple[AgentLoop, PendingChanges]:
    (tmp_path / "snake.html").write_text(TWO, encoding="utf-8")
    if pending is None:
        pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    register_edit_tools(registry, tmp_path, pending)  # write_file + confirm_rewrite
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=12,
            supervise=False,
        ),
    )
    return loop, pending


def _stage(tmp_path: Path, pending: PendingChanges) -> tuple[str, list[str]]:
    """摆出「第一步刚跑完」的现场：正文已暂存、清单还没交代。返回（令牌, 项 id）。"""
    (tmp_path / "snake.html").write_text(TWO, encoding="utf-8")
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": MERGED, "rewrite_reason": REASON}
    )
    assert result.ok is True and pending.staged_count() == 1
    record = pending.staged_items()[0]
    return record.token, list(record.ids())


def _all_text(loop: AgentLoop) -> str:
    return "\n".join(
        message.content
        for request in loop.gateway.requests
        for message in request.messages
    )


def test_暂存那一步不触发自动验证(tmp_path: Path) -> None:
    """暂存不是产出：文件还没落盘，验证只能报「没有需要验证的改动」。"""
    script = [
        _turn("重写", [_write()]),
        _turn("看一眼", [_read()]),
        _turn("完成", final="交给用户确认"),
    ]
    loop, pending = _build(tmp_path, script)
    calls: list = []
    loop.verify = lambda changed=(): (
        calls.append(1) or ToolResult(ok=True, content="通过")
    )

    loop.run("合并成单文档")

    assert calls == [], "暂存那一步不许触发验证（那是一句假通过）"
    assert pending.staged_count() == 1
    assert len(pending) == 0, "确认之前不许进待确认清单"
    # 令牌与「不要重发正文」都要发到模型面前，它才可能只发项 id。
    text = _all_text(loop)
    assert pending.staged_items()[0].token in text
    assert "不要把正文再发一遍" in text


def test_确认之后改动进清单并触发验证(tmp_path: Path) -> None:
    """确认（只发项 id）→ 改动真的进待确认清单 → 循环替它验一次。"""
    pending = PendingChanges(tmp_path)
    token, ids = _stage(tmp_path, pending)
    script = [
        _turn("确认", [{"name": "confirm_rewrite",
                        "arguments": {"token": token, "acked": ids}}]),
        _turn("完成", final="交给用户确认"),
    ]
    loop, pending = _build(tmp_path, script, pending=pending)
    calls: list = []
    loop.verify = lambda changed=(): (
        calls.append(1) or ToolResult(ok=True, content="通过")
    )

    loop.run("合并成单文档")

    assert calls, "确认落成改动之后必须替它验一次"
    assert len(pending) == 1, "确认之后改动进了待确认清单"
    assert pending.items()[0].new_text == MERGED
    assert pending.staged_count() == 0
    on_disk = (tmp_path / "snake.html").read_text(encoding="utf-8")
    assert on_disk != MERGED, "还在待确认清单里，没落盘"


def test_同一份正文原样重提被硬拦且话术带确认入口(tmp_path: Path) -> None:
    """工具层拦不住「重发正文」（参数一致就照收），循环层要拦住。"""
    first = _write()
    script = [
        _turn("重写", [first]),
        _turn("又发一遍", [first]),  # 参数一字未改
        _turn("完成", final="结束"),
    ]
    loop, pending = _build(tmp_path, script)
    loop.run("合并成单文档")

    text = _all_text(loop)
    assert "未执行" in text, "原样重提必须被硬拦"
    assert "已经收下并暂存" in text, "要说明白正文已经收下了"
    assert "只发项 id" in text
    # 拦下的话术里带着能照抄的 confirm 调用（令牌从暂存里取，不写死）。
    token = pending.staged_items()[0].token
    assert f'confirm_rewrite(token="{token}"' in text
    assert pending.staged_count() == 1, "重发那次不该产生第二份暂存"


def test_收窄成只能写时确认入口仍在(tmp_path: Path) -> None:
    """收窄把确认入口拿掉的话，模型刚被逼到只能写、却确认不了暂存的正文。"""
    loop, _ = _build(tmp_path, [_turn("完成", final="好了")])
    assert loop._commit_schema is not None
    allowed = loop._commit_schema["properties"]["tool_calls"]["items"][
        "properties"
    ]["name"]["enum"]
    assert "confirm_rewrite" in allowed
    assert "write_file" in allowed
