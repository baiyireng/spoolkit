"""P0 修②：LLM / 分词相关失败必须**响亮**，不许静默变成「未完成」。

真机对照那批的现场：代理把 `/tokenize` 劫成 502，整段格子被跑分器记成
「未完成」——与「任务没做完」长得一模一样，收敛率的分母里看不见它，
也没人知道该去查代理。

这一组用例守三件事：

1. 主循环把这一类失败接住，返回 `failed_reason` 与 `terminated_by="失败"`
   （**不是** `步数上限`），并在事件流里发一条带 kind 的失败标记；
2. 判词以 `RUN_FAILED_PREFIX` 开头（给读报告的人一句话分清两件事）；
3. **按类型收口**：不是模型/分词那一层的异常照旧往上抛——把它也变成安静
   失败，`dispatch` 那条「子智能体炸了主循环继续」的判据就不成立了。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import RUN_FAILED_PREFIX, AgentLoop
from spoolkit.config import Config
from spoolkit.errors import ContextOverflowError
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.llamacpp import LlamaCppError
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatResponse
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


class _Failing:
    """一个在 chat 上抛指定异常的网关（只在测试里用）。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def context_window(self) -> int | None:
        return 4096

    def chat(self, request):
        raise self.exc


class _OkOnce:
    """先正常应答一次，再抛异常——用来验证「中间那一步坏了」也是失败。"""

    def __init__(self, exc: BaseException, first: str) -> None:
        self.exc = exc
        self.first = first
        self.calls = 0

    def context_window(self) -> int | None:
        return 4096

    def chat(self, request):
        self.calls += 1
        if self.calls == 1:
            body = json.dumps(
                {
                    "thought": "读一下",
                    "tool_calls": [
                        {"name": "read_file", "arguments": {"path": "a.py"}}
                    ],
                    "state": None,
                    "done": False,
                    "final": None,
                },
                ensure_ascii=False,
            )
            tokenizer = OfflineTokenCounter()
            return ChatResponse(
                text=body,
                prompt_tokens=tokenizer.count(body),
                completion_tokens=tokenizer.count(body),
            )
        raise self.exc


def _build(tmp_path: Path, gateway, on_event=None) -> AgentLoop:
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    return AgentLoop(
        gateway=gateway,
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=4,
            supervise=False,
        ),
        on_event=on_event,
    )


def test_分词失败是显式失败而不是未完成(tmp_path: Path) -> None:
    """`LlamaCppError: tokenize 返回 502`（代理劫持那次的原话）。"""
    events: list[tuple[str, dict]] = []
    loop = _build(
        tmp_path,
        _Failing(LlamaCppError("tokenize 返回 502")),
        on_event=lambda kind, data: events.append((kind, data)),
    )
    result = loop.run("做点什么")

    assert result.failed_reason.startswith("LlamaCppError"), result.failed_reason
    assert "tokenize 返回 502" in result.failed_reason
    assert result.terminated_by == "失败", result.terminated_by
    assert result.concluded is False
    assert result.final.startswith(RUN_FAILED_PREFIX), result.final
    assert "不计入" in result.final, "判词没有把「失败」与「没做完」分开"
    # 事件流里那一条**带 kind 的失败标记**（跑分器按 kind 取，不靠正则）。
    failed_events = [data for kind, data in events if data.get("kind") == "run_failed"]
    assert failed_events, [kind for kind, _ in events]
    assert failed_events[0]["error_type"] == "LlamaCppError"
    assert failed_events[0]["ok"] is False


def test_中途那一步坏了同样记失败(tmp_path: Path) -> None:
    """第一步正常、第二步坏：不能因为「已经跑了几步」就把它算成没做完。"""
    loop = _build(
        tmp_path,
        _OkOnce(LlamaCppError("服务返回 502：（没有说明）"), "first"),
    )
    result = loop.run("做点什么")

    assert result.steps >= 1
    assert result.terminated_by == "失败"
    assert "502" in result.failed_reason
    assert result.final.startswith(RUN_FAILED_PREFIX)


def test_不是模型层的异常照旧往上抛(tmp_path: Path) -> None:
    """**收口的是这一类失败，不是所有失败**。

    网关壳自己写错（`RuntimeError`）不该被变成一条安静的「失败结果」：
    `dispatch` 那里有一条「子智能体炸了主循环继续」的判据，靠的正是
    这个异常能穿出去。
    """
    loop = _build(tmp_path, _Failing(RuntimeError("网关壳自己炸了")))
    try:
        loop.run("做点什么")
    except RuntimeError as exc:
        assert "网关壳自己炸了" in str(exc)
    else:  # pragma: no cover - 走到这里说明异常被吞了
        raise AssertionError("RuntimeError 被当成了模型层失败，判定太宽")


def test_上下文超长仍然走原来的重发路径(tmp_path: Path) -> None:
    """`ContextOverflowError` 是**可恢复**的一条路，不许被算成失败。

    它原先就在 `LlamaCppError` 之前单独接住（原样重发一次）。这条用例
    把它钉住：超长报错一次之后，第二轮正常收尾。
    """
    tokenizer = OfflineTokenCounter()

    class _OverflowThenDone:
        def __init__(self) -> None:
            self.calls = 0

        def context_window(self) -> int | None:
            return 4096

        def chat(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ContextOverflowError("提示词超过服务端上下文上限")
            body = json.dumps(
                {
                    "thought": "收尾",
                    "tool_calls": [],
                    "state": None,
                    "done": True,
                    "final": "看完了，没什么可改的。",
                },
                ensure_ascii=False,
            )
            return ChatResponse(
                text=body,
                prompt_tokens=tokenizer.count(body),
                completion_tokens=tokenizer.count(body),
            )

    loop = _build(tmp_path, _OverflowThenDone())
    result = loop.run("看看 a.py")

    assert result.failed_reason == "", result.failed_reason
    assert result.concluded is True
    assert result.terminated_by == "模型完成"
    assert result.resets >= 1, "超长之后没有丢历史重发"
