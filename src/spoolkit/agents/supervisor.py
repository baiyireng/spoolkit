"""督导会话：预算用尽或它在原地打转时，由它决定接着走、改道还是收手。

原先的做法是撞上步数上限就停，然后提示用户 `--resume` 再跑一次。那等于把
系统的判断成本推给用户，而且把一件事拆成两次对话——第二次还得从头理解一遍。
这里换成一个**独立上下文的会话**来判断：它看不到主循环的推理过程，只看证据
（目标、任务状态、最近发生了什么、机械统计），然后给一份结构化结论。

**为什么是一次调用，不是一个子智能体**：它不需要翻文件，需要的是判断。
一个紧凑的证据包加一份结构化结论，代价约等于主循环的一步，换回来的是一整轮
预算。让它自己去翻文件，只会把「判断」变成「又一次探索」——那正是它要判断
的那个问题。

**为什么机械信号还得留着**：督导是一次模型调用，会失败、也会看错。重复调用、
空回合、原地漫游这些**可检验**的信号仍然在循环里先挡一道；督导只在「机械层
已经说了有事、模型自己没纠正」的时候被叫起来——以及预算用尽时被叫一次。

两条安全线：

- 续期是有限的（总步数上限），否则一个卡住的任务能把 GPU 烧一整夜；
- 督导自己出问题（调用失败、结论解析不出来）一律退回原行为——它坏了不该让
  任务跟着坏，但这件事要说出来。
"""

import json
from dataclasses import dataclass, replace
from typing import Any

from spoolkit import limits as _limits
from spoolkit.context import templates as T
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.types import ChatRequest, Message

EXTEND = "extend"
REDIRECT = "redirect"
STOP = "stop"
# 「它已经做完了，只是自己没说」。
#
# 这条是实测逼出来的：审查者完成了核对、自动验证也通过了，但它在收尾前
# 用完了回合，于是循环记下的是「任务没做完，收手的原因：任务已完成」——
# 一句自相矛盾的话，而后果是这次审查被当成了「没得出结论」。
# 督导看得见证据，就该有办法说「它做完了」。
FINISH = "finish"

# 一次最多给多少步。给太多等于把上限取消掉：真正的用处是「够走完剩下的事」，
# 而它下一轮还会被问一次，不必一次给足。
#
# 默认值来自登记表（limits.KNOBS 的 supervisor_max_grant），这里留名字给外部
# 导入；实际用多少由调用方按本次运行的覆盖给（强模型可以一次多给些）。
MAX_GRANT = int(_limits.knob("supervisor_max_grant").default)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [EXTEND, REDIRECT, FINISH, STOP]},
        "steps": {"type": "integer"},
        "reason": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["action", "reason"],
}


@dataclass(frozen=True)
class Evidence:
    """给督导看的证据包。

    刻意不含主循环的对话历史与推理过程：它要判断的是「这些动作有没有进展」，
    读到推理过程只会让它附和那个推理——和审查者必须是独立上下文同一个理由。
    """

    goal: str
    step: int
    limit: int
    ceiling: int
    state: str = ""
    trace: tuple[str, ...] = ()
    edits: int = 0
    calls: int = 0
    resets: int = 0
    repeats: int = 0
    no_edit: int = 0
    tokens: int = 0
    asking: str = "预算用完了"
    # 只给它看最近这些行。再往前翻，它看到的是同一些动作的重复。
    trace_tail: int = 40

    @property
    def room(self) -> int:
        """总上限里还剩多少步。"""
        return max(0, self.ceiling - self.step)

    def render(self) -> dict[str, Any]:
        tail = self.trace[-self.trace_tail :]
        return {
            "goal": self.goal,
            "asking": self.asking,
            "step": self.step,
            "limit": self.limit,
            "ceiling": self.ceiling,
            "room": self.room,
            "state": self.state or "（它还没记录任何状态）",
            "trace": "\n".join(tail) if tail else "（还没有发生任何事）",
            "edits": self.edits,
            "calls": self.calls,
            "resets": self.resets,
            "repeats": self.repeats,
            "no_edit": self.no_edit,
            "tokens": self.tokens,
        }


@dataclass(frozen=True)
class Verdict:
    """督导的结论。"""

    action: str
    reason: str
    steps: int = 0
    message: str = ""
    # 问一次督导也是真花钱的。不记进来的话，用量报表会少算一笔，
    # 而调这块时看的正是那张表。
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def grants(self) -> int:
        """这次能给多少步（0 表示不续期）。"""
        if self.action in (EXTEND, REDIRECT):
            return self.steps
        return 0


def parse_verdict(
    text: str, evidence: Evidence, max_grant: int = MAX_GRANT
) -> Verdict | None:
    """把督导的一句话判成结论。解析不出来返回 None——调用方按「不敢续期」处理。

    含糊的续期比不续期危险：不续期只是停下来，而错误续期会把预算继续投进
    一个已经卡住的任务。所以这里的默认值是「不续」。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action", "")).strip().lower()
    if action not in (EXTEND, REDIRECT, FINISH, STOP):
        return None
    reason = str(payload.get("reason") or "").strip()
    message = str(payload.get("message") or "").strip()
    if action == FINISH:
        return Verdict(FINISH, reason or "证据显示任务已经完成", 0, message)

    raw = payload.get("steps")
    steps = int(raw) if isinstance(raw, (int, float)) else 0
    # 它想给多少不算数：上限由我们夹住。越权一次就等于上限不存在。
    steps = max(0, min(steps, max_grant, evidence.room))
    if action in (EXTEND, REDIRECT) and steps <= 0:
        return Verdict(STOP, reason or "总步数上限已经用完了，不能再续", 0, "")
    return Verdict(action, reason, steps, message)


def supervise(
    gateway: ModelGateway,
    evidence: Evidence,
    max_tokens: int = 512,
    max_grant: int = MAX_GRANT,
) -> Verdict | None:
    """问一次督导。

    调用失败或结论不合格式都返回 None。**不抛异常**：督导是增强，
    它坏了不该让整个任务跟着坏。
    """
    try:
        response = gateway.chat(
            ChatRequest(
                messages=(
                    Message(
                        role="user",
                        content=T.SUPERVISE.format(**evidence.render()),
                    ),
                ),
                max_tokens=max_tokens,
                response_schema=VERDICT_SCHEMA,
            )
        )
    except Exception:
        return None
    verdict = parse_verdict(response.text, evidence, max_grant)
    if verdict is None:
        return None
    return replace(
        verdict,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )
