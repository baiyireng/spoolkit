"""计划/自主这条路上的事件通道（D3）。

修的是什么：`run --plan` 与 `run --autonomous` 原先**一条事件都不发**——
`execute_step` 装配循环时没接 `on_event`，而这条路上每一句进度都 `print` 到
stdout 上。后果是两件事同时发生：

- `--events`（探针 / MCP / 网页壳）拿到的 stdout 上混着几十行散文，录下来的
  文件不能再直接读；解析器只能忽略它们，而"忽略"意味着**没有任何机制保证**
  下一次混进来的是可忽略的东西；
- 客户端看不到 START / STEP / FINAL：没有过程、没有第几步，最后连一条收尾
  都没有——一次正常的「没能拆出步骤」与"进程挂了"在事件流里长得一样。

判据（可证伪）：
1. START / STEP / FINAL / USAGE 四样都发得出来，次序对（START 最先、FINAL 最后、
   USAGE 在 FINAL 之前）；
2. **stdout 上每一行都是合法事件**（散文一句都不许在上面），散文换到 stderr；
3. 不发事件时（没给 `--events`）stdout 上不许出现 JSON 行——开关与 `run`
   那条路是同一个，不另立一套。
"""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from spoolkit.agents.plan import FAILED, Plan, PlanStep, plan_path, save_plan
from spoolkit.cli.commands.plan import advance_plan, autonomous
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.web.protocol import FINAL, KNOWN, START, STEP, TOOL, USAGE


def _plan_payload() -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "goal": "写 01_a/x.py",
                    "acceptance": "过",
                    "scope": ["01_a/"],
                    "executor": "self",
                }
            ]
        },
        ensure_ascii=False,
    )


def _write_turn(path: str, content: str) -> str:
    return json.dumps(
        {
            "thought": "写一份产出",
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": path, "content": content},
                    "why": "做这一步",
                }
            ],
            "state": None,
            "done": False,
            "final": None,
        },
        ensure_ascii=False,
    )


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _args(tmp_path: Path, **kwargs) -> argparse.Namespace:
    base = dict(
        goal="把工作区里的题都做对",
        scope="**",
        cover="",
        limit=10,
        batch=0,
        no_roll=False,
        policy="auto",
        window=4096,
        max_steps=2,
        subagent_steps=2,
        no_memory=True,
        session="cli",
        provider="fake",
        model="",
        base_url="",
        proxy="",
        script="",
        no_supervise=False,
        step_ceiling=0,
        allow_read=[],
        root=str(tmp_path),
        events=True,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _events_of(captured: str) -> list[dict]:
    """把 stdout 逐行解析成事件。**任何非 JSON 行都会在这里炸**——那正是判据。

    非 JSON 行要给一条能读的断言（而不是 `JSONDecodeError`）：混进来的正是
    diff 与散文，排错时想看的就是"混进来的是哪一行"。
    """
    out: list[dict] = []
    for line in captured.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"stdout 上出现了非事件行（散文 / diff 混进了事件流）：{line[:200]!r}"
            ) from exc
        assert payload.get("type") in KNOWN, payload
        out.append(payload)
    return out


def test_自主模式发四类事件且stdout只有JSON(tmp_path: Path, capsys) -> None:
    """一次真的会落盘的自主运行：事件齐全、编号唯一、**散文与 diff 全在 stderr**。

    这条同时钉四件事：
    1. START/STEP/FINAL/USAGE 四样都在，次序对；
    2. 循环自己的事件真的接上了（`tool` 事件来自 `AgentLoop`）——漏接
       `on_event` 时它会消失；
    3. STEP **只有一条编号空间**：计划路径那条（`n`/`total`/`goal`）。
       循环那条 `{"n": 0}` 的被挡掉了，否则消费方会画出「第 0 步」；
    4. auto 落盘时 diff 与人读留档走 stderr，stdout 上一个非事件字符都没有
       （`apply_with_audit` 的 `out` 必须一路传下去，这是实测抓到过的漏点）。
    """
    gateway = FakeModel(
        script=[
            _plan_payload(),
            _write_turn("01_a/x.py", "x = 1\n"),
            _turn("改好了"),
        ],
        tokenizer=OfflineTokenCounter(),
    )

    assert autonomous(_args(tmp_path), tmp_path, gateway) == 0

    captured = capsys.readouterr()
    events = _events_of(captured.out)
    kinds = [item["type"] for item in events]

    assert kinds[0] == START, kinds
    assert STEP in kinds and TOOL in kinds, kinds
    assert USAGE in kinds and FINAL in kinds, kinds
    assert kinds.index(USAGE) < kinds.index(FINAL), kinds
    assert kinds[-1] == FINAL, kinds

    start = events[0]
    assert start["mode"] == "autonomous"
    assert start["goal"] == "把工作区里的题都做对", start
    assert start["policy"] == "auto", start
    steps = [item for item in events if item["type"] == STEP]
    assert len(steps) == 1, steps
    assert steps[0]["n"] == 1 and steps[0]["total"] == 1, steps
    assert steps[0]["goal"] == "写 01_a/x.py", steps
    usage = next(item for item in events if item["type"] == USAGE)
    assert set(("steps", "calls", "prompt_tokens", "completion_tokens")) <= set(usage)
    final = events[-1]
    assert final["ok"] is True
    assert final["mode"] == "autonomous"
    assert final["completed"] == 1 and final["total"] == 1, final
    assert "自主运行结束" in final["text"]
    # 落盘那一笔的结算结果与撤销命令都在 FINAL 上（网页壳据此摆按钮）。
    assert final["settled"] == "auto", final
    assert final["revert_command"].startswith("spool revert --run "), final

    # 散文一句都没上 stdout（选的是只可能来自散文的那几句：FINAL 的 text 里
    # 本来就有「自主运行结束」，那是事件载荷，不是散文行）。`_events_of` 已经
    # 证明 stdout 上每一行都是合法事件——包括 diff：它绝不能出现在这里。
    assert "授权范围" not in captured.out and "拆解：" not in captured.out
    # **结构判据**（比找关键词可靠）：stdout 上不许出现任何非 JSON 行。
    # diff 的正文可能出现在某条事件的载荷里（`tool` 事件带调用参数），
    # 那是协议内容，不是散文行——所以判的是"这一行能不能解析成事件"。
    assert all(
        line.lstrip().startswith("{")
        for line in captured.out.splitlines()
        if line.strip()
    ), captured.out
    assert "授权范围" in captured.err and "拆解：" in captured.err
    # diff 与人读的留档确实还在，只是换了出口（`apply_with_audit` 的 `out`
    # 一路传下来才会这样；漏传时它们就回到 stdout 上了）。
    assert "+++ b/01_a/x.py" in captured.err, captured.err
    assert "已自动应用" in captured.err, captured.err


def test_没给events时stdout上一行JSON都不出现(tmp_path: Path, capsys) -> None:
    """开关必须与 `run` 那条路同一个：默认（散文模式）stdout 不该变成事件流。"""
    gateway = FakeModel(
        script=[_plan_payload(), _turn("改好了")],
        tokenizer=OfflineTokenCounter(),
    )

    assert autonomous(_args(tmp_path, events=False), tmp_path, gateway) == 0

    captured = capsys.readouterr()
    assert "{" not in captured.out, captured.out[:200]
    assert "自主运行结束" in captured.err


def test_推进一步也发事件且FINAL带这一步的结果(tmp_path: Path, capsys) -> None:
    plan = Plan(
        goal="把题做对",
        steps=[PlanStep(index=1, goal="改 01", acceptance="过", scope=("01_a/",))],
    )
    save_plan(plan_path(tmp_path, "cli"), plan)
    args = _args(tmp_path, batch=0)

    def fake_execute(ctx, step, scope, **kw):
        from spoolkit.agent.loop import LoopResult
        from spoolkit.agent.state import TaskState

        # **这里顺带钉住接线**：`execute_step` 被换掉了，但 ctx 必须把事件出口
        # 带到执行层——否则「接上了 on_event」这件事在用例里看不见。
        assert ctx.on_event is not None
        assert ctx.writer is not None
        result = LoopResult(
            finished=True,
            final="改好了",
            state=TaskState(task_id="t", goal=step.goal),
            steps=2,
            resets=0,
            trace=[],
            prompt_tokens=11,
            completion_tokens=5,
            model_calls=3,
        )
        ctx.totals.add(result)
        return result, None

    with patch("spoolkit.cli.commands.plan.execute_step", side_effect=fake_execute), \
         patch(
             "spoolkit.cli.commands.plan.record_step",
             # record_step 现在返回 `(结算结果, 撤销命令)`：两样都进 FINAL。
             return_value=("auto", "spool revert --run r1"),
         ):
        rc = advance_plan(args, tmp_path, object())

    assert rc == 0
    captured = capsys.readouterr()
    events = _events_of(captured.out)
    kinds = [item["type"] for item in events]
    assert kinds[0] == START and kinds[-1] == FINAL, kinds
    assert kinds.index(USAGE) < kinds.index(FINAL), kinds
    assert STEP in kinds and "执行第 1/1 步" not in captured.out
    usage = next(item for item in events if item["type"] == USAGE)
    # USAGE 是**整轮**的账（多步要累计），不是最后一步的：
    assert (usage["steps"], usage["calls"]) == (2, 3), usage
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (11, 5), usage
    final = events[-1]
    assert final["ok"] is True and final["mode"] == "plan"
    assert final["completed"] == 1 and final["total"] == 1, final
    assert final["settled"] == "auto", final
    assert final["revert_command"] == "spool revert --run r1", final


def test_计划被阻塞时也有START与FINAL(tmp_path: Path, capsys) -> None:
    """没有计划不是"进程消失了"：开工与收尾两条都要发出来。

    只发 FINAL 的话，按 START 建占位的客户端会看到一个没有开头的收尾
    （实测抓到的形状）；`steps` 未知时给 0，不许瞎编。
    """
    args = _args(tmp_path)

    assert advance_plan(args, tmp_path, object()) == 2

    events = _events_of(capsys.readouterr().out)
    assert [item["type"] for item in events] == [START, FINAL], events
    assert events[0]["mode"] == "plan" and events[0]["steps"] == 0, events[0]
    assert events[1]["ok"] is False
    assert "还没有计划" in events[1]["text"]


def test_阻塞的计划也发START与FINAL(tmp_path: Path, capsys) -> None:
    plan = Plan(
        goal="把题做对",
        steps=[
            PlanStep(
                index=1, goal="改 01", acceptance="过", scope=("01_a/",),
                status=FAILED, retries=1,
            )
        ],
    )
    save_plan(plan_path(tmp_path, "cli"), plan)

    assert advance_plan(_args(tmp_path), tmp_path, object()) == 1

    events = _events_of(capsys.readouterr().out)
    kinds = [item["type"] for item in events]
    assert kinds == [START, FINAL], kinds
    assert events[1]["ok"] is False
    assert events[1]["blocked_step"] == 1, events[1]
