"""确定性假模型。

只按预设脚本顺序应答，不模拟任何智能。用途只有测试与回放：
验证循环、预算、协议。它不能生成代码，也不能当「没模型也能干活」的兜底。
"""

from collections.abc import Callable, Sequence

from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse

# 假模型流式切片长度：够测增量，又不把单测拖成海量回调。
_STREAM_CHUNK = 8


class FakeModel:
    """按脚本应答的假模型。"""

    def __init__(
        self,
        script: Sequence[str],
        tokenizer: TokenCounter,
        window: int | None = None,
    ) -> None:
        self._script = list(script)
        self._tokenizer = tokenizer
        self._window = window
        self.requests: list[ChatRequest] = []
        self._cursor = 0

    def context_window(self) -> int | None:
        return self._window

    @property
    def remaining(self) -> int:
        """脚本中尚未被消费的条数。"""
        return len(self._script) - self._cursor

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self._cursor >= len(self._script):
            raise RuntimeError("假模型脚本已耗尽，请补充足够的应答条目")

        self.requests.append(request)
        text = self._script[self._cursor]
        self._cursor += 1

        prompt_tokens = sum(self._tokenizer.count(m.content) for m in request.messages)
        return ChatResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=self._tokenizer.count(text),
        )

    def chat_stream(
        self,
        request: ChatRequest,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        """按脚本整段取出后切成小片回调——测流式装配用，不假装真生成。"""
        response = self.chat(request)
        if on_delta is not None:
            text = response.text
            for index in range(0, len(text), _STREAM_CHUNK):
                try:
                    on_delta(text[index : index + _STREAM_CHUNK])
                except Exception:
                    pass
        return response

