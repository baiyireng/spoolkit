"""确定性假模型。

用途有二：一是在无 GPU 时驱动开发与测试；二是在自动化测试中精确
控制模型输出，从而验证循环、预算、协议等机制本身是否正确。
它不模拟任何智能，只按预设脚本顺序应答。
"""

from typing import Sequence

from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse


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

