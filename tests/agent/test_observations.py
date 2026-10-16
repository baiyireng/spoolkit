"""观察外置：超长文档的判断不靠把全文留在对话里。"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop, LoopResult, call_signature
from spoolkit.agent.state import TaskState
from spoolkit.config import Config
from spoolkit.context import templates as T
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.gateway import wait_line
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall
from spoolkit.web.protocol import WAIT
from spoolkit.web.runner import trace_line
from spoolkit.web.protocol import Event


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


def _build(tmp_path: Path, script: list) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=6,
            supervise=False,
            overrides={"observation_inline_lines": 2},
        ),
    )


def test_超长html整份读取留下结构事实且正文不进对话(tmp_path: Path) -> None:
    html = (
        "<html>\n<body>\nMARKER_ONE\n</body>\n</html>\n"
        "<html>\n<body>\nMARKER_TWO\n</body>\n</html>\n"
    )
    (tmp_path / "snake.html").write_text(html, encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn(
                "看结构",
                calls=[{"name": "read_file", "arguments": {"path": "snake.html"}}],
            ),
            _turn("说完", final="两份文档接在一起"),
        ],
    )
    result = loop.run("看看 snake.html 的结构")
    prompt = "\n".join(message.content for message in loop.gateway.requests[-1].messages)
    assert "MARKER_TWO" not in prompt
    assert "MARKER_ONE" not in prompt
    assert "又一个 <html>" in prompt
    assert "已记入观察" in prompt
    assert result.model_final == "两份文档接在一起"
    assert result.report_text.count("两份文档接在一起") == 1
    assert result.report_text.startswith(T.MODEL_VERDICT_PREFIX)

    loop._context_revision += 1
    call = ToolCall("read_file", {"path": "snake.html"})
    decision, reason, _cause = loop._guard_repeat_decision(call_signature(call), call)
    assert decision == "useless", reason
    assert "观察" in reason


def test_整段行号读取不把全文送回对话且压缩后不再重读(tmp_path: Path) -> None:
    """两份拼在一起的页面，用 1 到末行去读，正文不能进对话。

    现场：模型为了对比两份功能，用 start_line/end_line 把整份又读回来。
    全文进了素材，压缩把它裁掉，护栏就说「结果被丢掉」并放行再读。
    声明对照要留在观察里，这样不用拿着两份正文也能比出谁多了什么。
    """
    html = (
        "<html><body><script>\n"
        "function fromQueue(){}\n"
        "const stepMs=120;\n"
        "/* ONLY_IN_BODY_A */\n"
        "</script></body></html>\n"
        "<html><body><script>\n"
        "function gameOver(){}\n"
        "const speed=120;\n"
        "/* ONLY_IN_BODY_B */\n"
        "</script></body></html>\n"
    )
    (tmp_path / "snake.html").write_text(html, encoding="utf-8")
    last = len(html.splitlines())
    loop = _build(
        tmp_path,
        [
            _turn(
                "对比两份",
                calls=[{
                    "name": "read_file",
                    "arguments": {
                        "path": "snake.html",
                        "start_line": 1,
                        "end_line": last,
                    },
                }],
            ),
            _turn("说完", final="两份有互补"),
        ],
    )
    loop.run("对比两份代码，不要删掉其中一份")
    prompt = "\n".join(message.content for message in loop.gateway.requests[-1].messages)
    assert "ONLY_IN_BODY_A" not in prompt
    assert "ONLY_IN_BODY_B" not in prompt
    assert "fromQueue" in prompt
    assert "gameOver" in prompt
    assert "stepMs" in prompt
    assert "speed" in prompt
    assert "只在" in prompt
    assert "行的事实" in prompt
    notes = "\n".join(body for _key, _name, body in loop._material_notes)
    assert "ONLY_IN_BODY_A" not in notes
    assert "ONLY_IN_BODY_B" not in notes
    assert "已记入观察" in notes

    loop._context_revision += 1
    call = ToolCall(
        "read_file",
        {"path": "snake.html", "start_line": 1, "end_line": last},
    )
    decision, reason, _cause = loop._guard_repeat_decision(call_signature(call), call)
    assert decision == "useless", reason
    assert "观察" in reason
    assert "收窄" in reason
    assert "声明对照" in reason
    assert "压缩丢弃" not in reason
    asking = loop._supervisor_asking("同一个读取被停用了")
    assert "fromQueue" in asking
    assert "gameOver" in asking
    assert "不要判成「第二份还没读」" in asking


def test_按行读取的那一段留在观察里(tmp_path: Path) -> None:
    html = "<html>\n<body>\nKEEP_THIS_SPAN\n</body>\n</html>\n"
    (tmp_path / "snake.html").write_text(html, encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn(
                "取一段",
                calls=[{
                    "name": "read_file",
                    "arguments": {"path": "snake.html", "start_line": 3, "end_line": 3},
                }],
            ),
            _turn("说完", final="看到了"),
        ],
    )
    loop.run("取第三行")
    prompt = "\n".join(message.content for message in loop.gateway.requests[-1].messages)
    # 当轮工具结果里有这一行；观察注入和素材卡都不留正文。
    assert "KEEP_THIS_SPAN" in prompt
    assert "已取行段" not in prompt
    notes = "\n".join(body for _key, _name, body in loop._material_notes)
    assert "KEEP_THIS_SPAN" not in notes
    assert "不入素材" in notes

    loop._context_revision += 1
    call = ToolCall(
        "read_file",
        {"path": "snake.html", "start_line": 3, "end_line": 3},
    )
    decision, reason, _cause = loop._guard_repeat_decision(call_signature(call), call)
    assert decision == "refresh", reason


def test_答复里的结论只有一份() -> None:
    result = LoopResult(
        finished=True,
        final="你说结构不对。\n\n最终验证：通过",
        state=TaskState(task_id="t", goal="g"),
        steps=1,
        resets=0,
        model_final="你说结构不对。",
    )
    text = result.report_text
    assert text.count("你说结构不对。") == 1
    assert text.startswith(T.MODEL_VERDICT_PREFIX + "你说结构不对。")
    assert "最终验证：通过" in text


def test_心跳标明是谁在等() -> None:
    assert wait_line(15, 245) == "模型生成中，已等待 15s / 上限 245s"
    assert (
        wait_line(15, 491, "素材压缩")
        == "素材压缩：模型生成中，已等待 15s / 上限 491s"
    )
    line = trace_line(Event(WAIT, {"elapsed": 15, "limit": 491, "phase": "素材压缩"}))
    assert line is not None
    assert line["text"].startswith("素材压缩：")
