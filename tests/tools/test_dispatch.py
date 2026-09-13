"""会话内的派发入口。

主循环原先没有派发的能力——`--delegate` 是启动前判断一次。真实任务里
「该不该派、派哪一块」是做到一半才看得清的，所以入口得在会话里。

这里盯三件事：闸门（没有验收标准不许派）、改动落到**同一份**待确认里
（否则会出现两套 diff）、以及派发失败不把主循环带走。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.dispatch import dispatch_spec
from spoolkit.tools.edit import PendingChanges, register_edit_tools
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall


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


def _dispatch_turn(acceptance: str = "a.py 里 x 的值是 1") -> str:
    return _turn(
        "这块活派出去做",
        [
            {
                "name": "dispatch",
                "arguments": {
                    "goal": "把 a.py 里的 x 改成 1",
                    "acceptance": acceptance,
                    # targets 是必填的：没有它，批次大小无从判断，
                    # 所有跟件数有关的约束都落不了地。
                    "targets": ["a.py"],
                },
            }
        ],
    )


def _build(tmp_path: Path, script: list):
    """主循环与子智能体共用一个网关——它们本来就是同一个模型的两个上下文。"""
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(script=script, tokenizer=tokenizer)
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    register_edit_tools(registry, tmp_path, pending)
    config = Config(
        project_root=tmp_path,
        context_window=8192,
        max_steps=4,
        subagent_steps=4,
        supervise=False,
    )
    registry.register(dispatch_spec(gateway, registry, config, tokenizer))
    loop = AgentLoop(
        gateway=gateway, tokenizer=tokenizer, registry=registry, config=config
    )
    return loop, pending


def test_没有验收标准不允许派发(tmp_path: Path) -> None:
    """没有可执行的判断依据，实现者做到什么程度都算完成——这条闸门不能破。"""
    loop, pending = _build(
        tmp_path, [_dispatch_turn(""), _turn("那我自己来", [], final="好")]
    )
    loop.run("改 a.py")
    fed_back = "\n".join(
        message.content for message in loop.gateway.requests[-1].messages
    )
    assert "不能派发" in fed_back
    assert "验收标准" in fed_back
    # 闸门挡住了就不该真的跑一遍子智能体
    assert pending.items() == []


def test_派发把改动落进同一份待确认(tmp_path: Path) -> None:
    """子智能体的写完工具绑的是同一份 PendingChanges——只有一份 diff。"""
    loop, pending = _build(
        tmp_path,
        [
            _dispatch_turn(),
            _turn(
                "写文件",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "x = 1\n"},
                    }
                ],
            ),
            _turn("写完了", [], final="已把 a.py 改成 x = 1"),
            _turn("看过 diff，符合验收标准", [], final="改动正确"),
            json.dumps({"verdict": "pass", "reasons": ["x 的值是 1"]}),
            _turn("收到", [], final="派发完成"),
        ],
    )
    result = loop.run("改 a.py")

    assert result.finished is True
    # 改动进了主循环那一份待确认，而不是子智能体自己的某个副本
    assert [change.path for change in pending.items()] == ["a.py"]
    # 结论要带上「审查过了」和「别再自己写一遍」
    fed_back = "\n".join(
        message.content for message in loop.gateway.requests[-1].messages
    )
    assert "独立审查：通过" in fed_back
    assert "不要把" in fed_back or "不要自己再写一遍" in fed_back


def test_派发那一趟的账记进主循环(tmp_path: Path) -> None:
    """子智能体的调用也是这次任务花的钱。

    不记的话主循环会报「2 次调用」，而子智能体那一路（实现 + 审查 + 判定）
    一次都不在表里——那张表是调这块时唯一的账本。
    """
    loop, _ = _build(
        tmp_path,
        [
            _dispatch_turn(),
            _turn(
                "写文件",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "x = 1\n"},
                    }
                ],
            ),
            _turn("写完了", [], final="改好了"),
            _turn("看过了", [], final="没问题"),
            json.dumps({"verdict": "pass", "reasons": ["符合验收标准"]}),
            _turn("收到", [], final="派发完成"),
        ],
    )
    result = loop.run("改 a.py")
    # 主循环 2 次（派发那一轮 + 收尾）＋ 子智能体 4 次（实现 2、审查 1、判定 1）
    assert result.model_calls == 6
    assert result.prompt_tokens > 0


def test_审查不通过会带理由回来(tmp_path: Path) -> None:
    loop, _ = _build(
        tmp_path,
        [
            _dispatch_turn(),
            _turn(
                "写文件",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "x = 2\n"},
                    }
                ],
            ),
            _turn("写完了", [], final="写好了"),
            _turn("看过了", [], final="x 是 2，不是 1"),
            json.dumps(
                {
                    "verdict": "fail",
                    "reasons": ["值不对"],
                    "fix_goal": "把 x 改成 1",
                }
            ),
            # 审查不通过后，运行时会问一次「还要不要再派人修」。
            # 这里让它决定不修——本次要验的是**理由有没有带回来**。
            json.dumps(
                {"delegate": False, "reason": "不值得再派一次", "goal": "收尾"}
            ),
            _turn("知道了", [], final="收到审查意见"),
        ],
    )
    loop.run("改 a.py")
    fed_back = "\n".join(
        message.content for message in loop.gateway.requests[-1].messages
    )
    assert "独立审查：**不通过**" in fed_back
    assert "值不对" in fed_back


def test_派发失败不带走主循环(tmp_path: Path) -> None:
    """子智能体那边炸了，主循环还该继续——它自己也能做这块活。"""

    class Broken:
        def chat(self, request):
            raise RuntimeError("网关掉了")

    tokenizer = OfflineTokenCounter()
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    spec = dispatch_spec(
        Broken(),
        registry,
        Config(project_root=tmp_path, context_window=8192, supervise=False),
        tokenizer,
    )
    result = spec.handler({"goal": "做点什么", "acceptance": "跑通"})
    assert result.ok is False
    assert "派发没能跑起来" in result.content


def test_一次派太多当场被拦(tmp_path: Path) -> None:
    """规模问题该在**派之前**拦：派大了它只会烧光预算、交回半成品。

    实测那次派了 50 件进去，回来是一句无从行动的话，然后还被送去审查。
    """
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    spec = dispatch_spec(
        None,
        registry,
        Config(project_root=tmp_path, context_window=8192, subagent_steps=20),
        OfflineTokenCounter(),
    )
    result = spec.handler(
        {
            "goal": "把这 11 道题都修了",
            "acceptance": "每个目录跑 pytest 都通过",
            "targets": [f"{i:02d}_task" for i in range(1, 12)],
        }
    )
    assert result.ok is False
    assert "超出一次能派的上限" in result.content
    assert "拆成几批" in result.content


def test_不说清目标就派不了(tmp_path: Path) -> None:
    """件数是所有跟「批次大小」有关的约束的前提。

    实测那次 8 件的派发**没声明 targets**，于是「别超过 5 件」这类约束
    根本落不了地——日志里只留下「目标未声明」。
    """
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    spec = dispatch_spec(
        None,
        registry,
        Config(project_root=tmp_path, context_window=8192, subagent_steps=20),
        OfflineTokenCounter(),
    )
    # 走注册表：必填参数是**schema 层**拦的（模型真正走的就是这一层）
    registry.register(spec)
    result = registry.invoke(
        ToolCall("dispatch", {"goal": "改点什么", "acceptance": "跑通"})
    )
    assert result.ok is False
    assert "targets" in result.content


def test_超过提醒线只是提醒不是上限(tmp_path: Path) -> None:
    """这条界线**撤过一次**，原因值得留着。

    曾经按「大批次审不出结论」把它改成硬拦——那条证据后来被推翻了：真正的
    原因是督导缺少「它已经做完了」这种结论（`supervisor.FINISH`），同一个
    9 件派发在修好之后连过两次。**混杂观察不能当结论用。**
    """
    loop, _ = _build(
        tmp_path,
        [
            _turn(
                "派一批",
                [
                    {
                        "name": "dispatch",
                        "arguments": {
                            "goal": "把这 8 处都改对",
                            "acceptance": "逐个跑 pytest 通过",
                            "targets": [f"{i:02d}_x.py" for i in range(1, 9)],
                        },
                    }
                ],
            ),
            _turn(
                "写文件",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "x = 1\n"},
                    }
                ],
            ),
            _turn("写完了", [], final="改好了"),
            _turn("看过了", [], final="没问题"),
            json.dumps({"verdict": "pass", "reasons": ["符合验收标准"]}),
            _turn("收到", [], final="派发完成"),
        ],
    )
    loop.run("改 8 处")
    fed_back = "\n".join(
        message.content for message in loop.gateway.requests[-1].messages
    )
    # 没被拦下（照常派出去了），但拿到了提醒
    assert "超出一次能派的上限" not in fed_back
    assert "超过 5 件" in fed_back


def test_两个上限是配置而不是写死的常量(tmp_path: Path) -> None:
    """能力标定不是安全边界：本地小模型和远程强模型不是一回事。

    写死在工具里，等于替强模型砍掉能力——外部 API 的窗口、预算、判断力
    都比本机 27B 强得多，它一次完全可以带 20 件。
    """
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    wide = dispatch_spec(
        None,
        registry,
        Config(
            project_root=tmp_path,
            context_window=8192,
            overrides={"max_targets": 20, "review_limit": 15},
        ),
        OfflineTokenCounter(),
    )
    # 15 件在这个配置下不越上限
    result = wide.handler(
        {
            "goal": "把这 15 处都改对",
            "acceptance": "逐个跑测试通过",
            "targets": [f"{i:02d}.py" for i in range(15)],
        }
    )
    # 网关是 None，会走到「派发没能跑起来」——但**不是被上限拦下的**
    assert "超出一次能派的上限" not in result.content
    assert "派发没能跑起来" in result.content


def test_太大时给主循环的话是可行动的(tmp_path: Path) -> None:
    """「太大」要和「做砸了」分开说：一个要拆任务，一个要改代码。"""
    loop, _ = _build(
        tmp_path,
        [
            _dispatch_turn(),
            _turn("【太大】这块要读 20 个文件，我只有 4 步。建议拆成两批。",
                  [], final="【太大】建议拆成两批，先做前一半"),
            _turn("那我先自己做一半", [], final="好"),
        ],
    )
    loop.run("改 a.py")
    fed_back = "\n".join(
        message.content for message in loop.gateway.requests[-1].messages
    )
    assert "太大" in fed_back
    assert "拆小" in fed_back


def test_派发战绩会记下来并在下次判断时看得见(tmp_path: Path) -> None:
    """主循环对子智能体的认知原本是静态的两句话：不知道在这个工作区里派出去
    是赚是亏。教训记的是任务级成败，不是派发级——所以单独记一笔。

    而且只在「正好要判断」的两个时刻取用：问 `tool_help("dispatch")` 时、
    拆解前收集环境时。常驻提示词里一个字都不放（那是每个请求的税）。
    """
    loop, _ = _build(
        tmp_path,
        [
            _dispatch_turn(),
            _turn(
                "写文件",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "x = 1\n"},
                    }
                ],
            ),
            _turn("写完了", [], final="改好了"),
            _turn("看过了", [], final="没问题"),
            json.dumps({"verdict": "pass", "reasons": ["符合验收标准"]}),
            _turn("收到", [], final="派发完成"),
        ],
    )
    loop.run("改 a.py")

    log = (tmp_path / ".agent" / "dispatch-log.md").read_text(encoding="utf-8")
    assert "[通过]" in log
    assert "次调用" in log

    # 下一次**运行**要派时看得见：说明在装定时带上战绩
    # （同一次运行里不必刷新——它刚亲眼看过结果）
    fresh = dispatch_spec(
        loop.gateway,
        loop.registry,
        loop.config,
        OfflineTokenCounter(),
    )
    assert "本工作区记过的派发" in fresh.description
    assert "通过 1 次" in fresh.description


def test_没有战绩时不加那段废话(tmp_path: Path) -> None:
    loop, _ = _build(tmp_path, [_turn("好", [], final="好")])
    spec = dispatch_spec(
        loop.gateway, loop.registry, loop.config, OfflineTokenCounter()
    )
    assert "本工作区记过的派发" not in spec.description
