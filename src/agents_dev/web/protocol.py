"""事件协议。

子进程在 stdout 上一行吐一个 JSON 对象。非 JSON 行一律忽略——
内核偶尔会打印别的（第三方库的警告之类），不能因为一行杂音让整个流断掉。

未知类型也忽略，而不是报错。这样以后加新事件时，老版本的服务不会崩。
"""

import json
from dataclasses import dataclass, field
from typing import Any

START = "start"
STEP = "step"
TOOL = "tool"
DIFF = "diff"
AWAIT = "await"
CONFIRM = "confirm"
USAGE = "usage"
FINAL = "final"
ERROR = "error"

KNOWN = frozenset(
    {START, STEP, TOOL, DIFF, AWAIT, CONFIRM, USAGE, FINAL, ERROR}
)


@dataclass(frozen=True)
class Event:
    """一条事件。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> str:
        """序列化成一行，不带换行符。"""
        return json.dumps({"type": self.type, **self.data}, ensure_ascii=False)


def parse_line(line: str) -> Event | None:
    """解析一行。不是合法事件的都返回 None，不抛异常。"""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind not in KNOWN:
        return None
    return Event(
        type=kind,
        data={key: value for key, value in payload.items() if key != "type"},
    )

