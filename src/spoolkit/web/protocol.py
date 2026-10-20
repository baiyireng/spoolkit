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
# 系统说给用户听的一句话。不是工具调用——工具那行只报「成功/失败」，
# 而「你的交付物里有个数找不到出处」这种事，理由才是全部内容。
NOTE = "note"
WAIT = "wait"
# 面向人的正文增量（流式）。channel 先只实现 assistant。
DELTA = "delta"
# 纯文本终稿增量。不经 JSON `final` 抽取；老客户端忽略未知类型，这里登记后网页才收得到。
FINAL_DELTA = "final_delta"
# 长文一节的草稿开始 / 正式提交。重写只再发这一节，不重放前面的节。
SECTION_START = "section_start"
SECTION_DONE = "section_done"
# 任务时间线。与 tool / diff / await / final 并行，不替换它们。
TASK_EVENT = "task_event"

KNOWN = frozenset(
    {
        START,
        STEP,
        TOOL,
        DIFF,
        AWAIT,
        CONFIRM,
        USAGE,
        FINAL,
        ERROR,
        NOTE,
        WAIT,
        DELTA,
        FINAL_DELTA,
        SECTION_START,
        SECTION_DONE,
        TASK_EVENT,
    }
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

