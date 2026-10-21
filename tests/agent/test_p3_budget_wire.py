"""P3-1：主循环把上一轮实测的 prompt_tokens 传进控制预算。"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.context.phase_budget import output_tokens_for_turn
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _v2_tool() -> str:
    return json.dumps(
        {
            "thought": "看一下目录",
            "tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}],
            "done": False,
            "summary": "",
        },
        ensure_ascii=False,
    )


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    return registry


class _Recording(FakeModel):
    def __init__(self, holder: list, script: list[str], tokenizer: OfflineTokenCounter) -> None:
        super().__init__(script, tokenizer)
        self.holder = holder
        self.at_entry: list[int | None] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.at_entry.append(self.holder[0]._last_prompt_tokens)
        return super().chat(request)


def test_响应后记下实测且次轮用它算预算(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tokenizer = OfflineTokenCounter()
    holder: list[AgentLoop] = []
    # 第二次必须换路径。同一路径再列一次会被重复护栏拦住并退还步数，
    # 循环会多要一条脚本，测不到「下一次调用用上一次的实测」。
    second = json.dumps(
        {
            "thought": "看一下子目录",
            "tool_calls": [{"name": "list_dir", "arguments": {"path": "sub"}}],
            "done": False,
            "summary": "",
        },
        ensure_ascii=False,
    )
    gateway = _Recording(holder, [_v2_tool(), second], tokenizer)
    loop = AgentLoop(
        gateway=gateway,
        tokenizer=tokenizer,
        registry=_registry(tmp_path),
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1},
        ),
    )
    holder.append(loop)
    assert loop._last_prompt_tokens is None
    loop.run("看一下")
    assert len(gateway.requests) == 2
    measured = sum(tokenizer.count(item.content) for item in gateway.requests[0].messages)
    assert measured > 0
    assert gateway.at_entry[0] is None
    assert gateway.at_entry[1] == measured
    assert gateway.requests[1].max_tokens == output_tokens_for_turn(
        protocol_v2=True,
        legacy=loop._budget.output_reserve(),
        n_ctx=loop._budget.window,
        prompt_tokens=measured,
        messages=gateway.requests[1].messages,
        phase="control",
        safety_ratio=1.05,
    )
    assert gateway.requests[1].max_tokens <= 8192 - measured - 192
    assert gateway.requests[1].max_tokens <= 768
    assert loop._last_prompt_tokens == sum(
        tokenizer.count(item.content) for item in gateway.requests[1].messages
    )


def _old_turn(thought: str, calls=None, final=None) -> str:
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


def test_素材压缩重建历史后下一轮实测已清空(tmp_path: Path) -> None:
    """压缩把 history 换成压缩稿，上一轮 P_total 不能留给下一轮。"""
    log = tmp_path / "logs" / "run.log"
    log.parent.mkdir()
    failed = (
        "FAILED tests/services/test_services.py::test_case_{:02d} "
        "- AssertionError: assert state == 'stopped'"
    )
    noise = (
        "2026-10-06 12:00:{:02d} DEBUG poller tick={} queue=3 depth=7 "
        "worker=spool-worker-1 elapsed=0.03ms retry=0 "
        "note=heartbeat received from supervisor and the worker pool is healthy"
    )
    log.write_text(
        "\n".join([failed.format(i) for i in range(30)] + [noise.format(i, i) for i in range(8)])
        + "\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    tokenizer = OfflineTokenCounter()
    holder: list[AgentLoop] = []
    gateway = _Recording(
        holder,
        [
            _old_turn("先看日志", [{"name": "read_file", "arguments": {"path": "logs/run.log"}}]),
            _old_turn("够了", final="结论：同一条断言失败了三十次。"),
        ],
        tokenizer,
    )
    loop = AgentLoop(
        gateway=gateway,
        tokenizer=tokenizer,
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=2,
            supervise=False,
            overrides={"material_compact_ratio": 0.2, "material_compact_share": 0.2},
        ),
        compactor=lambda _prompt: "- [read_file logs/run.log] 三十条同一条断言失败",
    )
    holder.append(loop)
    result = loop.run("定位测试为什么挂")
    assert result.compactions == 1
    assert result.resets == 0
    assert any("素材压缩" in line for line in loop._trace)
    assert not any("上下文重置" in line for line in loop._trace)
    assert not any("上下文整理" in line for line in loop._trace)
    assert gateway.at_entry[0] is None
    assert gateway.at_entry[1] is None


def test_重置后下一轮请求时实测已清空(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    holder: list[AgentLoop] = []
    gateway = _Recording(holder, [_v2_tool(), _v2_tool()], tokenizer)
    loop = AgentLoop(
        gateway=gateway,
        tokenizer=tokenizer,
        registry=_registry(tmp_path),
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1, "hard_trigger_ratio": 0.01},
        ),
    )
    holder.append(loop)
    loop.run("看一下")
    assert any("上下文重置" in line for line in loop._trace)
    assert gateway.at_entry[1] is None


class _Cuts:
    def __init__(self, texts: list[tuple[str, bool]]) -> None:
        self._texts = list(texts)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        text, truncated = self._texts.pop(0)
        return ChatResponse(
            text=text,
            prompt_tokens=400,
            completion_tokens=20,
            truncated=truncated,
        )

    def chat_stream(self, request: ChatRequest, on_delta=None) -> ChatResponse:
        return self.chat(request)


def test_v2控制截断只请求一次且反馈仍在(tmp_path: Path) -> None:
    broken = '{"thought":"写一大段","tool_calls":[{"name":"list_dir","arguments":{"path":"."'
    gateway = _Cuts([(broken, True), (_v2_tool(), False)])
    loop = AgentLoop(
        gateway=gateway,
        tokenizer=OfflineTokenCounter(),
        registry=_registry(tmp_path),
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1},
        ),
    )
    loop.run("看一下")
    assert len(gateway.requests) == 2
    assert gateway.requests[0].max_tokens <= 768
    assert gateway.requests[1].max_tokens <= 768
    assert gateway.requests[1].max_tokens != gateway.requests[0].max_tokens * 2
    second = "\n".join(item.content for item in gateway.requests[1].messages)
    assert "拆小" in second
