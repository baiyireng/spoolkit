"""被拦下（没有执行）的尝试：**不烧预算** + **强制换路**。

这一组测试盯的两件事都是实测出来的（探针 l5-set3 的三条 rename 现场）：

- `rename-cross-file-r0`：`find_symbol` 带着未知参数被硬拦一次，**照样吃掉 1 步**
  （8 步预算少一步），因为退还被写成了**按工具名分类**的白名单，而它不在名单里。
- `rename-cross-file-r1` / `merge-html-r0`：同一条 `search_code` 被硬拦 3 次，
  模型每一轮的意图都写着「已经按要求改好了」，而参数一字未改。护栏每次都端回
  **同一句话**——而它对同一句话是免疫的。

所以这里的断言只有三条口径：
1. 「这一步一个调用都没执行」= 不占步数预算，**与工具名无关**，且退还在上限内、
   在 trace 与事件流里看得见；
2. 同一个（工具 + 参数）被拦到 `reroute_block_at` 次之后**停用**，话术换成
   「停用 + 替代动作清单」，**不再是同一句话**；
3. 停用之后**交给督导**（带上被拦次数与已经给过的替代方案），不让它自己空转到上限。

变异口径（改哪一处会让哪条红）写在每条用例的 docstring 里。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult, ToolSpec

#: 一个**不在旧退还白名单里**的工具名（旧白名单 = 编辑类 + read_file / list_dir /
#: search_code / survey）。用它才测得出「按工具名分类」这个口径已经被去掉。
BAD_TOOL = "find_symbol"


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _registry() -> ToolRegistry:
    """一个会失败的 `find_symbol`：失败过之后再原样发，就会被硬拦。"""
    registry = ToolRegistry()

    def handler(args):
        if args.get("name") == "get_rows":
            return ToolResult(ok=False, content="找不到符号: get_rows")
        return ToolResult(ok=True, content=f"找到符号: {args.get('name')}")

    registry.register(
        ToolSpec(
            name=BAD_TOOL,
            description="按名字找符号",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "符号名"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    return registry


def _build(tmp_path: Path, script: list, events: list | None = None, **overrides):
    config_kwargs = {
        "project_root": tmp_path,
        "context_window": 4096,
        "max_steps": int(overrides.pop("max_steps", 12)),
        "supervise": bool(overrides.pop("supervise", False)),
    }
    if overrides:
        config_kwargs["overrides"] = overrides
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=_registry(),
        config=Config(**config_kwargs),
        on_event=(lambda kind, data: events.append((kind, data))) if events is not None else None,
    )


def _bad(name: str = "get_rows") -> dict:
    return {"name": BAD_TOOL, "arguments": {"name": name}}


def _all_seen(loop: AgentLoop) -> str:
    """模型与督导**每一次**请求里读到的全部内容（拦截话术就在这里）。"""
    return "\n".join(
        message.content
        for request in loop.gateway.requests
        for message in request.messages
    )


def _refusals(loop: AgentLoop) -> set[str]:
    """这一次运行里**发出去过的**拦截话术（按内容去重）。

    去重是必须的：同一句话会在之后每一次请求的历史里再出现一遍，
    直接数出现次数会把「端回去 1 次」数成「3 次」。
    """
    return {
        message.content
        for request in loop.gateway.requests
        for message in request.messages
        if "未执行" in message.content
    }


def test_被拦的一步不占预算_与工具名无关(tmp_path: Path) -> None:
    """`find_symbol` 不在旧白名单里，被拦那一步同样不该吃预算。

    变异：把退还条件改回 `any(call.name in _REFUND_TOOLS ...)`（按工具名分类），
    这条会红——被拦那一步会占掉 1 步，`result.steps` 变成 3。
    """
    loop = _build(
        tmp_path,
        [
            _turn("先找一次", [_bad()]),
            _turn("原样再发", [_bad()]),
            _turn("算了", final="结束"),
        ],
    )
    result = loop.run("找 get_rows")

    assert any("不占步数预算" in line for line in result.trace), result.trace
    # 第一次调用真的执行了（失败也算一步），被拦的那一步退还 → 只走 2 步
    # （第二步是被拦后它自己宣告结束的那一步）。
    assert result.steps == 2, f"被拦的一步吃掉了预算：steps={result.steps}"


def test_退还省下真预算_且有上限(tmp_path: Path) -> None:
    """N 次连续被拦不会把预算烧光：退还真的省下步数，但省得有上限。

    两个跑法共用同一份脚本，只有 `blocked_refund_limit` 不同：0（不退还）与默认 3。
    变异：去掉退还（或去掉上限）→ 两个读数会相等（或退还变成无限），这条红。
    """
    script = [_turn("先找一次", [_bad()])] + [
        _turn(f"原样第 {i} 次", [_bad()]) for i in range(1, 7)
    ]
    script += [
        _turn("换一个符号", [_bad("other")]),
        _turn("完成", final="结束"),
    ]

    loop_a = _build(tmp_path, script, max_steps=20, blocked_refund_limit=0)
    no_refund = loop_a.run("找 get_rows")
    loop_b = _build(tmp_path, script, max_steps=20)
    refunded = loop_b.run("找 get_rows")

    refunds = [line for line in refunded.trace if "不占步数预算" in line]
    assert any("已退还 1/" in line for line in refunds)
    # 上限 = blocked_refund_limit（默认 3）：退到上限之后照常计预算。
    assert len(refunds) == 3, f"退还步数超出上限：{refunds}"
    assert not any("不占步数预算" in line for line in no_refund.trace)
    assert refunded.steps < no_refund.steps, (
        f"退还没有省下预算：退还 {refunded.steps} 步 vs 不退还 {no_refund.steps} 步"
    )
    # 「仍能推进」：被拦了 6 次之后它还是走到了另一条调用上。
    assert "找到符号: other" in _all_seen(loop_b)


def test_同一组合被拦到阈值后停用并给出替代清单(tmp_path: Path) -> None:
    """第 N 次被拦之后**换的必须是它读到的东西**，不是同一句话再说一遍。

    变异：把 `_refuse` 里的停用分支去掉（每次都端回原话），
    「停用」与「替代动作清单」都不会出现，这条红。
    """
    events: list = []
    loop = _build(
        tmp_path,
        [
            _turn("先发一次", [_bad()]),
            _turn("原样 1", [_bad()]),
            _turn("原样 2", [_bad()]),
            _turn("原样 3", [_bad()]),
            _turn("算了", final="结束"),
        ],
        events=events,
    )
    result = loop.run("找 get_rows")

    seen = _all_seen(loop)
    refusals = _refusals(loop)
    heads = [one.splitlines()[0] for one in refusals]
    assert "停用" in seen, "第二次被拦没有换成停用话术"
    assert "替代动作清单" in seen, "停用话术里没有可选动作清单"
    # 「不再是同一句话」：常规那句**只发出过一次**（第一次被拦），从第 2 次起
    # 换成停用话术。判据看每一句的第一行：停用话术里会「备查」原话，按整段数
    # 会把那句话数两遍。
    assert len(refusals) == 3, f"预期三次拦截：{heads}"
    assert sum(1 for one in heads if "这个调用与上一次" in one) == 1, (
        f"第二次被拦端回去的还是同一句话：{heads}"
    )
    assert sum(1 for one in heads if "停用" in one) == 2, (
        f"停用话术没有覆盖后面的每一条：{heads}"
    )
    kinds = [data.get("kind") for kind, data in events if kind == "note"]
    assert "combo_disabled" in kinds, "停用这件事在事件流里看不见"
    assert any("不占步数预算" in line for line in result.trace)


def test_停用之后把轨迹交给督导(tmp_path: Path) -> None:
    """它仍然反复 → 交给督导，并带上「被拦几次 + 已经给过哪些替代方案」。

    变异：去掉 `stuck_on_reroute` 那条触发条件，督导不会在这一步被叫起来，
    「请判断：这件事还该不该做」这句就不会出现在任何一次请求里，这条红。
    """
    redirect = json.dumps(
        {
            "action": "redirect",
            "reason": "先别重发，换一条路",
            "steps": 3,
            "message": "换一个符号名再找，或者直接给结论。",
        },
        ensure_ascii=False,
    )
    verdict = json.dumps(
        {"action": "stop", "reason": "同一个组合已停用，收尾"},
        ensure_ascii=False,
    )
    # 脚本顺序 = 真实调用顺序：
    #   ① 第一次真失败（进失败表）→ ② 原样重提，第 1 次被拦（常规话术，还没停用）
    #   → ③ 步首叫督导（「同一动作连续失败」那条），督导让它改道
    #   → ④ 它又原样发一次：这一次**停用**并留下交给督导的证据
    #   → ⑤ 步首再叫督导，这一次问的就是「组合已停用，这件事还该不该做」。
    loop = _build(
        tmp_path,
        [
            _turn("先发一次", [_bad()]),
            _turn("原样 1", [_bad()]),
            redirect,
            _turn("原样 2", [_bad()]),
            verdict,
        ],
        supervise=True,
    )
    result = loop.run("找 get_rows")

    seen = _all_seen(loop)
    assert "请判断：这件事还该不该做" in seen, "停用之后没有把轨迹交给督导"
    assert "已经给过的替代方案" in seen, "交给督导的证据里少了已经给过的替代方案"
    assert "已经被硬拦 2 次" in seen, "交给督导的证据里少了被拦次数"
    assert result.finished is False


def test_拦截话术带反向条款且不读成禁止删除(tmp_path: Path) -> None:
    """§2.5 的反向条款：拦的是**这一次动作**，不是「不许删文件 / 不许精简」。

    变异：删掉 `_reverse_clause()`，两次话术里都没有这一句，这条红。
    """
    from spoolkit.agent.loop import _combo_disabled_message, _retry_block_message
    from spoolkit.tools.types import ToolCall

    call = ToolCall(name=BAD_TOOL, arguments={"name": "get_rows"})
    retry = _retry_block_message(call, "找不到符号: get_rows")
    disabled = _combo_disabled_message(call, 2, ["find_symbol(name=\"other\")"], "找不到符号")
    for text in (retry, disabled):
        assert "拦的是**这一次动作**" in text
        assert "不是「不许删文件" in text
        assert "删已被替代的文件都是正常编辑" in text
    # 逐字回显参数：它以为自己改过，这一行是它唯一能看出「没改」的地方。
    assert '{"name": "get_rows"}' in retry
