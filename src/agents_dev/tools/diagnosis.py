"""给 Agent 的诊断申请工具。

它只能做两件事：登记问题、读回报告。**不能自己出报告**——报告要由具备
真实环境权限的一侧写，并带签名；没签名的报告读回来会明确标注来源无法验证。

这条边界是刻意的。前面几轮实测里，模型编造过「llama.cpp 需要 llama-cpp-python」
这种听起来很专业的事实。如果它还能自己写一份「环境有问题，忽略这些失败」的
诊断报告再据此行动，那就是把编造换了个地方发生。
"""

from pathlib import Path

from agents_dev import diagnosis
from agents_dev.tools.types import ToolResult, ToolSpec


def _request(root: Path, args: dict) -> ToolResult:
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolResult(ok=False, content="question 不能为空：要查什么，写清楚")
    request = diagnosis.store_request(
        root,
        question=question,
        hypothesis=str(args.get("hypothesis") or ""),
        evidence=str(args.get("evidence") or ""),
    )
    return ToolResult(
        ok=True,
        content=(
            f"已登记诊断请求 {request.id}。\n"
            "它需要由具备真实环境权限的会话验证——这一步你自己做不到。\n"
            "你可以先用 read_diagnosis 看它有没有回来，或把情况说明给用户。"
            "不要停下来空等：能推进的部分先推进。"
        ),
    )


def _read(root: Path, args: dict) -> ToolResult:
    request_id = str(args.get("id") or "").strip()
    if not request_id:
        items = diagnosis.load_requests(root)
        if not items:
            return ToolResult(ok=True, content="还没有登记过诊断请求。")
        lines = [
            f"{item.id} [{'已回复' if item.answered else '等待验证'}] {item.question}"
            for item in items
        ]
        return ToolResult(ok=True, content="\n".join(lines))

    report, verified = diagnosis.read_report(root, request_id)
    if not report:
        return ToolResult(
            ok=False, content=f"诊断请求 {request_id} 还没有报告。"
        )
    head = "【来源已验证】" if verified else "【来源无法验证】"
    body = [
        f"{head} 诊断报告 {request_id}",
        f"结论：{report.get('verdict', '')}",
        f"发现：{report.get('findings', '')}",
    ]
    if report.get("evidence"):
        body.append(f"证据：{report['evidence']}")
    if not verified:
        body.append(
            "这份报告没有有效签名，不能作为依据——不要据此认定为环境问题。"
        )
    return ToolResult(ok=verified, content="\n".join(body))


def request_diagnosis_spec(root: Path) -> ToolSpec:
    """登记一个「怀疑是环境或工具本身有问题」的诊断请求。"""
    return ToolSpec(
        name="request_diagnosis",
        description=(
            "当你怀疑失败的原因是环境或工具本身（不是你的改动），"
            "而现有工具又无法证实时，用它登记一个问题，交给具备真实环境权限的"
            "会话去验证。它只产出报告，不改变你的权限，也不是绕过限制的手段"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要验证什么"},
                "hypothesis": {"type": "string", "description": "你怀疑是什么原因"},
                "evidence": {"type": "string", "description": "你观察到的具体现象"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        handler=lambda args: _request(root, args),
        brief="登记环境问题求验证",
        group="诊",
    )


def read_diagnosis_spec(root: Path) -> ToolSpec:
    """查看诊断请求与报告。"""
    return ToolSpec(
        name="read_diagnosis",
        description=(
            "不传 id 时列出全部诊断请求及其状态；传 id 时读回报告。"
            "报告只有签名有效才可信，来源无法验证的不要当依据"
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "请求编号，留空则列全部"}
            },
            "additionalProperties": False,
        },
        handler=lambda args: _read(root, args),
        brief="读诊断报告",
        group="诊",
    )
