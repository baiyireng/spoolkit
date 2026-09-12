"""工具输出台账 + 数字核对工具。

台账由循环在每次工具调用后追加（见 AgentLoop），模型改不了它——它自己
写进去的「出处」不算出处，那样这道检查就废了。

模型侧只提供读：拿自己的草稿来核对，看哪些数字找不到出处。
"""

from agents_dev.check.numbers import mismatched, render, untraceable
from agents_dev.tools.types import ToolResult, ToolSpec


class SourceLog:
    """本次运行里所有工具输出的原始文本。"""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._facts: list = []

    def add(self, text: str, facts=()) -> None:
        if text:
            self._items.append(text)
        for fact in facts or ():
            self._facts.append(fact)

    def items(self) -> tuple[str, ...]:
        return tuple(self._items)

    def facts(self) -> tuple:
        return tuple(self._facts)

    def clear(self) -> None:
        self._items.clear()
        self._facts.clear()


def check_numbers_spec(log: SourceLog) -> ToolSpec:
    """核对草稿里的数字有没有出处。"""

    def handler(args: dict) -> ToolResult:
        text = str(args.get("text") or "")
        if not text.strip():
            return ToolResult(ok=False, content="text 不能为空：把要核对的草稿放进来")
        claims = untraceable(text, log.items(), log.facts())
        wrong = mismatched(text, log.facts())
        return ToolResult(ok=True, content=render(claims, wrong))

    return ToolSpec(
        name="check_numbers",
        description=(
            "核对一段草稿里的数字：逐个回到本次运行的工具输出里找出处，"
            "找不到的列出来。写报告或结论之前用它过一遍——"
            "你自己心算出来的数不会有出处"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要核对的草稿"}
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="核对草稿里的数字",
        group="核",
    )
