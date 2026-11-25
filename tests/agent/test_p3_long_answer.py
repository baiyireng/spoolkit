"""P3-3：v2 长文走大纲、逐节、组装；旧协议不发章节事件。"""

import json
import re
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.writing_state import OUTLINE_INSTRUCTION
from spoolkit.config import Config
from spoolkit.llm.types import ChatRequest, ChatResponse
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.registry import ToolRegistry

_TITLES = ("背景", "问题", "方案", "步骤", "风险")


def _control() -> str:
    return json.dumps(
        {
            "thought": "这题要分节写",
            "tool_calls": [],
            "done": True,
            "summary": "无需外部信息。按五节展开。",
            "answer_plan": {"mode": "long", "target_tokens": 2000, "sections": 5},
        },
        ensure_ascii=False,
    )


class _Long:
    def __init__(
        self,
        *,
        outline: str | None = None,
        final: str | None = None,
        section: str | None = None,
    ) -> None:
        self.requests: list[ChatRequest] = []
        self.outline = outline or "\n".join(
            f"{title} | 写清这一节 | 400" for title in _TITLES
        )
        self.final = final or "# 短终稿\n\n改走短答。"
        self.section = section
        self._tokenizer = OfflineTokenCounter()

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        blob = "\n".join(item.content for item in request.messages)
        if "不是控制层" in blob:
            text = self.outline
        elif "只写第" in blob:
            if self.section:
                text = self.section
            else:
                matched = re.search(r"「(.+?)」", blob)
                title = matched.group(1) if matched else "本节"
                text = f"只属于{title}的正文。"
        elif "进入终稿阶段" in blob:
            text = self.final
        else:
            text = _control()
        return ChatResponse(
            text=text,
            prompt_tokens=sum(self._tokenizer.count(item.content) for item in request.messages),
            completion_tokens=self._tokenizer.count(text),
        )

    def chat_stream(self, request: ChatRequest, on_delta=None) -> ChatResponse:
        response = self.chat(request)
        if on_delta is not None and response.text:
            on_delta(response.text)
        return response


def _loop(tmp_path: Path, gateway: _Long, *, v2: bool, events: list) -> AgentLoop:
    return AgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=32768,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1 if v2 else 0},
        ),
        on_event=lambda kind, data: events.append((kind, dict(data))),
    )


def test_v2长文五节齐全且后节提示不带前节正文(tmp_path: Path) -> None:
    events: list = []
    gateway = _Long()
    loop = _loop(tmp_path, gateway, v2=True, events=events)
    result = loop.run("写一份说明")
    assert "不是控制层" in OUTLINE_INSTRUCTION
    kinds = [kind for kind, _data in events]
    assert kinds.count("section_start") == 5
    assert kinds.count("section_done") == 5
    for title in _TITLES:
        assert f"## {title}" in result.final
        assert f"只属于{title}的正文" in result.final
    section_requests = [
        request
        for request in gateway.requests
        if any("只写第" in item.content for item in request.messages)
    ]
    assert len(section_requests) == 5
    sizes = [
        sum(loop.tokenizer.count(item.content) for item in request.messages)
        for request in section_requests
    ]
    assert sizes[-1] <= sizes[0] + 80
    for earlier, later in zip(section_requests, section_requests[1:]):
        earlier_blob = "\n".join(item.content for item in earlier.messages)
        later_blob = "\n".join(item.content for item in later.messages)
        title = next(item for item in _TITLES if f"「{item}」" in earlier_blob)
        assert f"只属于{title}的正文" not in later_blob
    outline = next(
        request
        for request in gateway.requests
        if any("不是控制层" in item.content for item in request.messages)
    )
    assert outline.messages[:-1] == gateway.requests[0].messages


def test_长文终稿含文件宣称和代码块仍收尾(tmp_path: Path) -> None:
    events: list = []
    code = "\n".join(f"x{index} = {index}" for index in range(25))
    section = f"已完成 docs/design.md。\n```python\n{code}\n```"
    gateway = _Long(section=section)
    loop = _loop(tmp_path, gateway, v2=True, events=events)
    result = loop.run("修复 loop.py 的预算")
    assert [kind for kind, _data in events].count("section_start") == 5
    assert "docs/design.md" in result.final
    assert not any("结论没有落盘" in line for line in result.trace)
    assert result.finished is True


def test_大纲失败时仍用固定三节分节(tmp_path: Path) -> None:
    events: list = []
    gateway = _Long(outline="这里没有大纲")
    loop = _loop(tmp_path, gateway, v2=True, events=events)
    result = loop.run("写一份说明")
    kinds = [kind for kind, _data in events]
    assert kinds.count("section_start") == 3
    for title in ("背景与目标", "方案与结构", "风险与下一步"):
        assert f"## {title}" in result.final
    assert "短终稿" not in result.final
    assert not any("长文没写成" in line for line in result.trace)


def test_旧协议即使像长文也不发章节事件(tmp_path: Path) -> None:
    events: list = []
    gateway = _Long()
    loop = _loop(tmp_path, gateway, v2=False, events=events)
    goal = "写一份详细报告。" + ("内容" * 2000)
    # 旧协议的完成仍在 JSON final 里。网关的控制分支返回 v2 JSON，这里换成旧 final。
    gateway.chat = _old_chat(gateway)  # type: ignore[method-assign]
    result = loop.run(goal)
    assert len(gateway.requests) == 1
    assert "写完了" in result.final
    assert not any(kind.startswith("section_") for kind, _data in events)


def _old_chat(gateway: _Long):
    def chat(request: ChatRequest) -> ChatResponse:
        gateway.requests.append(request)
        text = json.dumps(
            {
                "thought": "短答即可",
                "tool_calls": [],
                "state": None,
                "done": True,
                "final": "写完了",
            },
            ensure_ascii=False,
        )
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=4)

    return chat
