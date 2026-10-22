"""任务事件和工具结果的投影契约。

这一轮只定义信封和纯函数。主循环仍按原来的 content 回灌，
不在这里改 runner、界面或任何工具的行为。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from spoolkit.tools.types import Artifact, ToolResult

# 运行时按字符截断。分词上限只给契约测试用服务端 /tokenize 核对，
# 不在这里做离线估算。
SUMMARY_MAX_CHARS = 240
SUMMARY_TOKEN_CEILING = 400

PHASES = frozenset({
    "understand",
    "act",
    "observe",
    "decide",
    "verify",
    "final",
    "error",
    "retrieval",
    "approval",
})
SOURCES = frozenset({"model", "tool", "system", "prefetch", "user"})

FIELD_ORDER = (
    "task_id",
    "run_id",
    "turn_id",
    "seq",
    "step",
    "phase",
    "source",
    "tool_call_id",
    "tool",
    "intent",
    "args_digest",
    "status",
    "summary",
    "refs",
    "artifacts",
    "duration",
    "risk",
)


def clip_summary(text: str) -> str:
    """把给模型的摘要压进字符上限。短文本原样返回。"""
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    if SUMMARY_MAX_CHARS <= 1:
        return text[:SUMMARY_MAX_CHARS]
    return text[: SUMMARY_MAX_CHARS - 1] + "…"


def adapt_legacy(result: ToolResult) -> ToolResult:
    """把旧工具结果收成新信封。原对象不动。

    summary 用截断后的 summary；没填就用截断后的 content。
    content、raw、data、artifacts、next、facts、usage 照抄。
    raw 留给界面和证据。模型只通过 project_for_model 看 summary。
    """
    summary = result.summary or result.content
    return ToolResult(
        ok=result.ok,
        content=result.content,
        facts=result.facts,
        usage=result.usage,
        raw=result.raw,
        summary=clip_summary(summary),
        data=dict(result.data),
        artifacts=result.artifacts,
        next=result.next,
    )


def project_for_model(result: ToolResult, *, thought: str = "") -> str:
    """模型只看见 summary；没填时退回截断后的 content。

    raw、thought、data、artifacts、next 都不拼进去。thought 这个参数
    只是让调用方能证明那段思考被丢掉了。
    """
    del thought
    text = result.summary if result.summary else result.content
    return clip_summary(text)


@dataclass(frozen=True)
class TaskEvent:
    """一轮里按 seq 排好的一条事件。没有 thought 字段，意图放在 intent。"""

    task_id: str
    run_id: str
    turn_id: str
    seq: int
    step: int
    phase: str
    source: str
    tool_call_id: str = ""
    tool: str = ""
    intent: str = ""
    args_digest: str = ""
    status: str = ""
    summary: str = ""
    refs: tuple[str, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    duration: float | None = None
    risk: str = ""

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"未知 phase：{self.phase}")
        if self.source not in SOURCES:
            raise ValueError(f"未知 source：{self.source}")
        object.__setattr__(self, "summary", clip_summary(self.summary))

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "seq": self.seq,
            "step": self.step,
            "phase": self.phase,
            "source": self.source,
            "tool_call_id": self.tool_call_id,
            "tool": self.tool,
            "intent": self.intent,
            "args_digest": self.args_digest,
            "status": self.status,
            "summary": self.summary,
            "refs": list(self.refs),
            "artifacts": [
                {"type": item.type, "path": item.path, "ref": item.ref}
                for item in self.artifacts
            ],
            "duration": self.duration,
            "risk": self.risk,
        }
        if tuple(payload) != FIELD_ORDER:
            raise RuntimeError("TaskEvent 字段顺序和 FIELD_ORDER 不一致")
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def ordered(events: Sequence[TaskEvent]) -> tuple[TaskEvent, ...]:
    """按 seq、再按 step 排。同一对键保持输入里的相对次序。"""
    return tuple(sorted(events, key=lambda item: (item.seq, item.step)))


def tool_event(
    *,
    task_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    step: int,
    tool_call_id: str,
    tool: str = "",
    intent: str = "",
    args_digest: str = "",
    status: str = "",
    summary: str = "",
    refs: tuple[str, ...] = (),
    artifacts: tuple[Artifact, ...] = (),
    duration: float | None = None,
    risk: str = "",
) -> TaskEvent:
    """一次成功的工具观察。失败走 error_from_tool，不在这里另开类型。"""
    return TaskEvent(
        task_id=task_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        step=step,
        phase="observe",
        source="tool",
        tool_call_id=tool_call_id,
        tool=tool,
        intent=intent,
        args_digest=args_digest,
        status=status,
        summary=summary,
        refs=refs,
        artifacts=artifacts,
        duration=duration,
        risk=risk,
    )


def retrieval_event(
    *,
    task_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    step: int,
    summary: str,
    refs: tuple[str, ...] = (),
) -> TaskEvent:
    """预取必须落成事件，不能只活在提示词里。"""
    return TaskEvent(
        task_id=task_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        step=step,
        phase="retrieval",
        source="prefetch",
        summary=summary,
        refs=refs,
        status="ok",
    )


def approval_event(
    *,
    task_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    step: int,
    tool_call_id: str,
    status: str,
    summary: str,
    artifacts: tuple[Artifact, ...] = (),
) -> TaskEvent:
    """diff / await 通过同一个 tool_call_id 挂回那次工具调用。"""
    return TaskEvent(
        task_id=task_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        step=step,
        phase="approval",
        source="system",
        tool_call_id=tool_call_id,
        status=status,
        summary=summary,
        artifacts=artifacts,
    )


def error_from_tool(
    *,
    task_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    step: int,
    tool_call_id: str,
    summary: str,
    tool: str = "",
) -> TaskEvent:
    """工具 ok=false 收成 phase=error，不另造错误通道。"""
    return TaskEvent(
        task_id=task_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        step=step,
        phase="error",
        source="tool",
        tool_call_id=tool_call_id,
        tool=tool,
        status="error",
        summary=summary,
    )


def error_from_final(
    *,
    task_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    step: int,
    summary: str,
) -> TaskEvent:
    """final ok=false 收成 phase=error。"""
    return TaskEvent(
        task_id=task_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        step=step,
        phase="error",
        source="system",
        status="error",
        summary=summary,
    )


def error_from_exception(
    *,
    task_id: str,
    run_id: str,
    turn_id: str,
    seq: int,
    step: int,
    exc: BaseException,
) -> TaskEvent:
    """未接住的异常同样收成 phase=error。"""
    return TaskEvent(
        task_id=task_id,
        run_id=run_id,
        turn_id=turn_id,
        seq=seq,
        step=step,
        phase="error",
        source="system",
        status="error",
        summary=f"{type(exc).__name__}: {exc}",
    )
