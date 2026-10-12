"""督导接进主循环之后：撞上限不再等于失败。

原先撞上步数上限就停，然后提示用户 `--resume` 再跑一次——那等于把系统的
判断成本推给用户，还把一件事拆成两次对话。现在由一个独立上下文的会话判断：
续期、改道、还是收手。这个文件测的是**接点**：谁在什么时候被叫起来、
它的话怎么进到模型面前、它坏了会怎么样。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _turn(thought: str, calls=None, final=None, why=None, assessment=None) -> str:
    """一轮脚本回合。

    `why` 是给**每个**调用补的意图（想省事时一次给全部），`assessment`
    是它对上一次督导建议的表态。两者都能省——省了就是「模型没写」，
    而那本身也是一种要被看见的状态。
    """
    payload = {
        "thought": thought,
        "tool_calls": calls or [],
        "state": None,
        "done": final is not None,
        "final": final,
    }
    if why is not None and payload["tool_calls"]:
        payload["tool_calls"] = [
            {**call, "why": why} for call in payload["tool_calls"]
        ]
    if assessment is not None:
        payload["assessment"] = assessment
    return json.dumps(payload, ensure_ascii=False)


def _verdict(action: str, **rest) -> str:
    return json.dumps({"action": action, **rest}, ensure_ascii=False)


def _look() -> dict:
    return {"name": "list_dir", "arguments": {"path": "."}}


def _read() -> dict:
    return {"name": "read_file", "arguments": {"path": "a.py"}}


def _build(tmp_path: Path, script: list, **kwargs) -> AgentLoop:
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    registry.register(read_file_spec(tmp_path))
    settings = {"context_window": 4096, "max_steps": 2, **kwargs}
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, **settings),
    )


def test_撞上限时督导续期而不是停(tmp_path: Path) -> None:
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict("extend", steps=3, reason="每一步都在读到新的东西"),
            _turn("改好了", final="改好了"),
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert result.final == "改好了"
    assert any("督导续 3 步" in line for line in result.trace)


def test_督导说收手就以它的理由收尾(tmp_path: Path) -> None:
    """只说「未完成」的话，用户能做的只有原样再来一次。"""
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict("stop", reason="缺一个读不到的目录，这条路走不通"),
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is False
    assert "缺一个读不到的目录" in result.final


def test_督导不可用时退回原行为(tmp_path: Path) -> None:
    """脚本在督导那一步耗尽，对应真实里的「网关报错 / 返回的不是 JSON」。"""
    loop = _build(tmp_path, [_turn("看", [_look()]), _turn("看", [_look()])])
    result = loop.run("做点什么")
    assert result.finished is False
    # 说清是**谁**没给出结论：不然用户会以为是模型不想做了。
    assert "督导" in result.final


def test_关掉督导就回到硬上限(tmp_path: Path) -> None:
    loop = _build(
        tmp_path,
        [_turn("看", [_look()]), _turn("看", [_look()])],
        supervise=False,
    )
    result = loop.run("做点什么")
    assert result.final == "已达步数上限，任务未完成"
    # 一次多余的调用都没有——这是「关掉」该有的样子。
    assert len(loop.gateway.requests) == 2


def test_续期不会越过总上限(tmp_path: Path) -> None:
    """总上限是安全线：没有它，一个卡住的任务能把 GPU 烧一整夜。"""
    script: list = []
    for _ in range(3):
        script.append(_turn("看", [_look()]))
        script.append(_verdict("extend", steps=50, reason="还在推进"))
    loop = _build(tmp_path, script, max_steps=1, step_ceiling=3)
    result = loop.run("做点什么")
    assert result.steps == 3
    assert result.finished is False


def test_打转时督导改道并把话送到模型面前(tmp_path: Path) -> None:
    """机械层只挡「参数一模一样」的重复；换着花样绕要有人看得出来。"""
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict(
                "redirect",
                steps=2,
                reason="连着四次读同一个目录，没有提出任何改动",
                message="别再看目录了，直接读 a.py，然后说明你要改哪一行",
            ),
            _turn("明白", [], final="好了"),
        ],
        max_steps=8,
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert any(
        "别再看目录了" in message.content
        for request in loop.gateway.requests
        for message in request.messages
    )


def test_督导那次调用也算进用量(tmp_path: Path) -> None:
    """它真花钱。不记进来的话，用量表会少算一笔，而调这块看的正是那张表。"""
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict("extend", steps=3, reason="还在推进"),
            _turn("好了", final="好了"),
        ],
    )
    result = loop.run("做点什么")
    assert result.model_calls == len(loop.gateway.requests)


def test_换着花样绕也逃不掉(tmp_path: Path) -> None:
    """重复调用检测只盖得住「参数一模一样」的打转。

    实测那条轨迹 12 步里换了 8 种不同的调用，机械计数器一次都没响过。
    这里每一步读的文件都不同，重复计数永远是 1——只有「一直没产出」这个
    信号还在涨。

    但**这些读全都成功了**（每个文件都存在），所以按新的判据它们属于
    正常探索，督导被叫起来时问的不再是「它只看不写」——**意图**要看的是
    「拿到了新信息还是同一个坑里反复试」。这条用例里它每一轮读的、
    **每一轮想的「下一步」都是一个新的文件名**，那正是「换着花样绕」。
    """
    for name in "abcdefgh":
        (tmp_path / f"{name}.py").write_text("x = 0\n", encoding="utf-8")
    script = [
        _turn(f"翻 {name}.py", [{"name": "read_file", "arguments": {"path": f"{name}.py"}}])
        for name in "abcdefgh"
    ]
    script.append(
        _verdict(
            "redirect",
            steps=2,
            reason="连续九步只看不写，任务要的是改动",
            message="信息够了，直接用 write_file 改 a.py",
        )
    )
    script.append(_turn("明白", [], final="好了"))

    loop = _build(tmp_path, script, max_steps=20)
    result = loop.run("改一下")
    assert result.finished is True
    # 督导是在漫游到第 8 步时被叫起来的，不是等到 20 步烧完；而且问的
    # 那句话必须说准——八次读取**都成功了**，它是「先读后计划」，
    # 不是「只看不写、什么都没有」。
    assert any("连续 8 步只看不写" in line for line in result.trace), result.trace
    assert any("像是先读后计划" in line for line in result.trace), result.trace
    assert any("这一步按**正常探索**交给督导" in line for line in result.trace)


def test_每一步都拿到新信息时督导不会被无产出叫起来(tmp_path: Path) -> None:
    """**用户那条轨迹的核心**：先读后计划不是打转。

    「优化一下旧文件的代码」这类目标本来就要先读再计划；把它当打转、
    督导据此建议收手，等于把正常的探索掐死。

    阈值压到 3 是为了让**漫游判据**先于「预算用完」那一支触发：默认 8 的话，
    小预算下督导会先在「预算用完」上被叫起来，这条就测不到探索分支了
    （踩过一次：用例绿着，判据根本没执行）。
    """
    for name in "abcdefgh":
        (tmp_path / f"{name}.py").write_text("x = 0\n", encoding="utf-8")
    script = [
        _turn(f"翻 {name}.py", [{"name": "read_file", "arguments": {"path": f"{name}.py"}}])
        for name in "abcdefgh"
    ]
    script.append(_verdict("stop", reason="该收了"))
    script.append(_turn("收尾", [], final="好了"))

    loop = _build(
        tmp_path, script, max_steps=20, overrides={"no_edit_intervene_at": 3}
    )
    result = loop.run("优化一下旧文件的代码")
    asked = [line for line in result.trace if "叫督导" in line]
    assert asked, result.trace
    # 每一步都拿到了新内容 → 问的是「像是先读后计划」，不是「只看不写」。
    assert any("像是先读后计划" in line for line in asked), asked
    assert not any("一次都没有拿到新内容" in line for line in asked), asked


def test_窗口里夹着落空就不算探索() -> None:
    """`still_exploring` 的判据是 **all** 而不是 any。

    这里直接钉判据本身，因为写反了不会有任何报错：只会让「先读后计划」
    这个说法被用在一段其实在空转的窗口上，而督导据此续期——预算就投进了
    一个什么都没看到的循环。集成路径上这两种写法的差别很容易被别的分支
    盖住（试过三次，前两次的用例绿着而分支根本没执行）。
    """
    from spoolkit.agent.loop import still_exploring

    assert still_exploring([True, True, True]) is True
    assert still_exploring([True, False, True]) is False, "一次落空被后面的一次成功抵消了"
    assert still_exploring([False, False]) is False
    assert still_exploring([]) is False, "什么都没看过时不许说它在探索"


def test_每一步都拿到新信息时问的是先读后计划(tmp_path: Path) -> None:
    """窗口里每步都成功 → 报给督导的说法是「像是先读后计划」。"""
    for name in "abcdefgh":
        (tmp_path / f"{name}.py").write_text("x = 0\n", encoding="utf-8")
    script = [
        _turn(f"翻 {name}.py", [{"name": "read_file", "arguments": {"path": f"{name}.py"}}])
        for name in "abcdefgh"
    ]
    script.append(_verdict("stop", reason="该收了"))
    script.append(_turn("收尾", [], final="好了"))

    loop = _build(
        tmp_path, script, max_steps=20, overrides={"no_edit_intervene_at": 3}
    )
    result = loop.run("优化一下旧文件的代码")
    asked = [line for line in result.trace if "叫督导" in line]
    assert any("像是先读后计划" in line for line in asked), asked


def test_看了好几步却什么也没看到时按白看报给督导(tmp_path: Path) -> None:
    """反过来：每一步换一个不存在的文件，读**全失败**、一步新信息都没进来。

    这才是那类该被看见的漫游，问督导的话必须说「一次都没拿到新内容」。
    这条单独关掉「同一个失败动作」那一条判据——它比漫游先越线（两次失败
    就够），两条判据各测各的才说得清是谁在说话。读的名字也**避开 a.py**：
    夹具里那个文件是存在的，它一被读到，全失败的前提就不成立了。
    """
    script = [
        _turn(f"看 {name}", [{"name": "read_file", "arguments": {"path": f"{name}.py"}}])
        for name in "bcdefghi"
    ]
    script.append(_verdict("redirect", steps=2, reason="一直在白看"))
    script.append(_turn("明白", [], final="好了"))

    loop = _build(tmp_path, script, max_steps=20, overrides={"failed_repeat_at": 0})
    result = loop.run("改一下")
    asked = [line for line in result.trace if "叫督导" in line]
    assert any("一次都没有拿到新内容" in line for line in asked), asked
    # 每一次的参数都不同，机械层的重复计数永远归零——这条只可能由漫游判据
    # 抓出来，不是因为「它在重复调用」。
    assert not any("同一个失败动作" in line for line in asked), asked


def test_同一个失败动作反复重试会被单独叫给督导(tmp_path: Path) -> None:
    """用户说的「后一种才算打转」：同一个**失败**动作反复重试。

    参数一字不改的重提由机械层硬拦（动作没执行），但「它想干那件事」
    没被拦下——那是要给判断的事。这条用例里它每一步换一个不存在的文件名，
    指纹每次不同、机械层抓不到，只有「连续失败」这个信号在涨。
    """
    script = [
        _turn(f"看 {name}", [{"name": "read_file", "arguments": {"path": f"{name}.py"}}])
        for name in "abcdefgh"
    ]
    script.append(_verdict("redirect", steps=2, reason="同一个失败动作重复了 4 次"))
    script.append(_turn("明白", [], final="好了"))

    loop = _build(tmp_path, script, max_steps=20)
    result = loop.run("改一下")
    asked = [line for line in result.trace if "叫督导" in line]
    assert any("同一个动作已经连续失败" in line for line in asked), asked


def test_意图会被记进轨迹(tmp_path: Path) -> None:
    """工具调用必须带上「想干什么」。

    没有它，同一个失败在记录里只有工具名与参数——分不清「工具不完善」
    还是「它理解错了」，也就没法顺着它的思路排查。
    """
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn(
                "先看它的内容",
                [{"name": "read_file", "arguments": {"path": "a.py"}}],
                why="看 a.py 里现在是什么样",
            ),
            _turn("好了", [], final="好了"),
        ],
    )
    result = loop.run("优化一下")
    assert any("意图: 看 a.py 里现在是什么样" in line for line in result.trace)


def test_工具事件里带意图(tmp_path: Path) -> None:
    """`--events` 录下来的 JSONL 是排查的第一手材料，意图必须在里面。"""
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    seen: list[tuple[str, dict]] = []
    loop = _build(
        tmp_path,
        [
            _turn(
                "先看内容",
                [{"name": "read_file", "arguments": {"path": "a.py"}}],
                why="读 a.py 当前内容",
            ),
            _turn("好了", [], final="好了"),
        ],
    )
    loop.on_event = lambda kind, data: seen.append((kind, data))
    loop.run("优化一下")
    tools = [data for kind, data in seen if kind == "tool"]
    assert tools[0]["intent"] == "读 a.py 当前内容"


def test_督导建议要求表态且允许带理由拒绝(tmp_path: Path) -> None:
    """督导的话是**建议**，不是命令。

    注入时必须①写清这是建议、②要求用 assessment 表态、③写明不接受
    是允许的。缺任何一条，它都只能照做——而「照做了没有」不是我们能
    判断的东西。
    """
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("看目录", [_look()]),
            _turn("看目录", [_look()]),
            _turn("看目录", [_look()]),
            _turn("看目录", [_look()]),
            _verdict(
                "redirect",
                steps=2,
                reason="连着四次读同一个目录",
                message="别再看目录了，直接读 a.py",
            ),
            _turn("明白", [], final="好了"),
        ],
        max_steps=8,
    )
    result = loop.run("做点什么")
    prompt = "\n".join(
        message.content
        for request in loop.gateway.requests
        for message in request.messages
    )
    assert "别再看目录了" in prompt
    assert "建议" in prompt and "不是命令" in prompt
    assert "assessment" in prompt
    assert "不接受是允许的" in prompt


def test_它带着理由拒绝督导时表态被记下来(tmp_path: Path) -> None:
    """接受 / 不接受都要留下理由——否则「它为什么没照做」无从查起。"""
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("看目录", [_look()]),
            _turn("看目录", [_look()]),
            _turn("看目录", [_look()]),
            _turn("看目录", [_look()]),
            _verdict("redirect", steps=2, reason="连着四次读同一个目录",
                     message="别再看目录了，直接读 a.py"),
            _turn("明白了", [], final="好了", assessment="不接受：任务要求的是优化，我先确认现状"),
        ],
        max_steps=8,
    )
    result = loop.run("做点什么")
    assert any(
        "它对此前督导建议的表态：不接受" in line for line in result.trace
    ), result.trace


def test_督导判可收尾时由模型写结论(tmp_path: Path) -> None:
    """finish 把话交给模型写 final，不把督导的话当成模型结论。

    现场：材料已经够，督导 action=finish，message 是「请直接写 final」。
    旧行为当场收尾，model_final 为空，报告里那句是督导对模型说的话。
    """
    message = "请基于已有事实直接写 final，不要再读文件。"
    written = "两份实现可以互补：一边有输入队列，一边有结束层。"
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict("finish", reason="结论已可写出", message=message),
            _turn("写完", final=written),
        ],
    )
    result = loop.run("做点什么")
    assert result.model_final == written
    assert result.concluded
    assert result.finished is True
    prompt = "\n".join(item.content for item in loop.gateway.requests[-1].messages)
    assert message in prompt
    assert "直接给出结论" in prompt
    assert "请用 assessment" not in prompt


def test_督导交还后它仍调工具则不执行并沿用原话(tmp_path: Path) -> None:
    """交还的这一轮只接受结论。再读文件不能执行，否则又回到分段读取。"""
    message = "请直接写 final。"
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict("finish", reason="结论已可写出", message=message),
            _turn("再看", [_look()]),
        ],
    )
    result = loop.run("做点什么")
    assert result.model_final == ""
    assert not result.concluded
    assert result.finished is True
    assert message in result.final
    assert any("调用未执行" in line for line in result.trace)
    assert len(loop.gateway.requests) == 4


def test_督导说完成后按成功收尾(tmp_path: Path) -> None:
    """「事情已经做完、只是它自己没宣告」要有出路——这条是实测逼出来的。

    审查者完成了核对、自动验证也通过，但它在收尾前用完了回合，于是循环记下
    「任务没做完，收手的原因：任务已完成」——一句自相矛盾的话，而后果是这次
    工作被记成失败（派发路径里表现为「审查没得出结论」）。
    """
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict("finish", reason="自动验证已通过，该做的都做了"),
            _turn("写完", final="自动验证已通过，该做的都做了"),
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert result.model_final == "自动验证已通过，该做的都做了"
    assert any("交给模型写 final" in line for line in result.trace)


def test_名字环触发督导(tmp_path: Path) -> None:
    """交替 list_dir / read_file 时签名每次都不同，重复计数抓不到。

    四拍之后名字环是 list_dir→read_file；督导要被叫醒，且 asking 得说清
    这是短环——否则它只会在 trace 里看到「又读了一次」。
    """
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("读", [_read()]),
            _turn("看", [_look()]),
            _turn("读", [_read()]),
            _verdict("stop", reason="短环打转，这条路走不通"),
        ],
        max_steps=20,
    )
    result = loop.run("做点什么")
    asked = [line for line in result.trace if "叫督导" in line]
    assert asked, result.trace
    assert any("短环" in line for line in asked), asked


def test_短环续期后还会再叫督导(tmp_path: Path) -> None:
    """问过一次把门槛加倍，但换参回路还在转时必须还能再叫。

    把短环压成 0/1 再加倍，1>=2 会永远为假——write_file↔run_command
    换参回路只能叫醒一次。连续计数（像 no_edit_steps）才能在续期后
    再次越阈。

    脚本要按**督导吃哪几条**排：主循环每步吃 1 条，督导每被叫一次也吃 1 条
    （实测这条形状下是第 4、9 条）。所以续期与收手的结论必须正好压在那两个
    位置上；夹在中间的每一步都用**不同签名**的调用（换目录 / 换文件），
    免得被重复护栏拦下——那会把短环的连续计数打断，测的就不是这条判据了。
    每一步都必须**成功**：失败会走「同一个动作连续失败」那条升级路径，
    把督导的位置挪走。门槛每叫一次就 `max(streak*2, at*2)` 加倍：1 → 4，
    所以第二次越阈要多走满一圈，这里给足了。
    """
    for name in ("d0", "d1", "d2", "d3", "d4", "d5", "d6"):
        (tmp_path / name).mkdir(exist_ok=True)
    for name in ("b0.py", "b1.py", "b2.py", "b3.py", "b4.py", "b5.py", "b6.py"):
        (tmp_path / name).write_text("y = 1\n", encoding="utf-8")
    slots = {
        4: _verdict("extend", steps=10, reason="先给几步看它能不能自己停"),
        9: _verdict("stop", reason="还在短环里，收手"),
    }
    dirs = [".", "d0", "d1", "d2", "d3", "d4", "d5", "d6"]
    files = ["b0.py", "b1.py", "b2.py", "b3.py", "b4.py", "b5.py", "b6.py"]
    script: list[str] = []
    for index in range(16):
        if index in slots:
            script.append(slots[index])
        elif index % 2 == 0:
            step = index // 2
            script.append(
                _turn(
                    "看",
                    [{"name": "list_dir", "arguments": {"path": dirs[step % len(dirs)]}}],
                )
            )
        else:
            step = index // 2
            script.append(
                _turn(
                    "读",
                    [{"name": "read_file", "arguments": {"path": files[step % len(files)]}}],
                )
            )
    loop = _build(tmp_path, script, max_steps=20, supervise=True, step_ceiling=40)
    result = loop.run("做点什么")
    asked = [line for line in result.trace if "叫督导" in line and "短环" in line]
    assert len(asked) >= 2, result.trace


def test_督导说完成时带上它给的话(tmp_path: Path) -> None:
    loop = _build(
        tmp_path,
        [
            _turn("看", [_look()]),
            _turn("看", [_look()]),
            _verdict(
                "finish",
                reason="都做完了",
                message="8 个目录逐个跑 pytest 都通过，任务已完成。",
            ),
            _turn("写完", final="8 个目录逐个跑 pytest 都通过，任务已完成。"),
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is True
    # 督导的话进下一轮提示，结论由模型写。只读运行不该拖一句
    # 「这次没改过任何文件」。
    prompt = "\n".join(item.content for item in loop.gateway.requests[-1].messages)
    assert "8 个目录逐个跑 pytest 都通过，任务已完成。" in prompt
    assert result.model_final == "8 个目录逐个跑 pytest 都通过，任务已完成。"
    assert result.final == "8 个目录逐个跑 pytest 都通过，任务已完成。"


def test_暂存之后再读会说明磁盘仍是旧文件(tmp_path: Path) -> None:
    """大重写先暂存，read_file 仍读磁盘。回执必须说明这不是暂存失败。"""
    from spoolkit.tools.edit import PendingChanges, write_file_spec

    old = "".join(f'<p id="p{index}">ONLY_ON_DISK {index}</p>\n' for index in range(12))
    new = "<html>\n<p>STAGED_BODY</p>\n</html>\n"
    (tmp_path / "snake.html").write_text(old, encoding="utf-8")
    pending = PendingChanges(tmp_path)
    staged = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": new, "rewrite_reason": "合并重复段落"}
    )
    assert staged.ok, staged.content[:200]
    token = pending.staged_items()[0].token
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("看", [{"name": "read_file", "arguments": {"path": "snake.html"}}]),
                _turn("记下"),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=2, supervise=False
        ),
        pending=pending,
    )
    loop.run("合并")
    prompt = "\n".join(item.content for item in loop.gateway.requests[-1].messages)
    assert "ONLY_ON_DISK" in prompt
    assert "STAGED_BODY" not in prompt
    assert "暂存未落盘" in prompt
    assert token in prompt
    assert "confirm_rewrite" in prompt
    assert "不要再 write_file" in prompt
    assert (tmp_path / "snake.html").read_text(encoding="utf-8") == old


def test_收尾交还时确认暂存并写出结论(tmp_path: Path) -> None:
    """finish 不能要求把全文再写进 final，也不能丢掉 confirm_rewrite。"""
    from spoolkit.tools.edit import PendingChanges, confirm_rewrite_spec, write_file_spec

    old = "".join(f'<p id="p{index}">第 {index} 行</p>\n' for index in range(12))
    new = "<html>\n<p id=\"p0\">第 0 行</p>\n</html>\n"
    (tmp_path / "snake.html").write_text(old, encoding="utf-8")
    pending = PendingChanges(tmp_path)
    staged = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": new, "rewrite_reason": "合并重复段落"}
    )
    assert staged.ok, staged.content[:200]
    record = pending.staged_items()[0]
    token = record.token
    dropped = [f"{one}：重复段落，合并时去掉了" for one in record.ids()]
    written = "合并完成：重复的第二份已去掉。"
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(confirm_rewrite_spec(tmp_path, pending))
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("看", [{"name": "read_file", "arguments": {"path": "snake.html"}}]),
                _verdict(
                    "finish",
                    reason="合并版已经暂存",
                    message="请把合并后的 snake.html 完整代码写进 final。",
                ),
                _turn(
                    "确认清单",
                    [{
                        "name": "confirm_rewrite",
                        "arguments": {"token": token, "dropped": dropped},
                    }],
                    final=written,
                ),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=1, supervise=True
        ),
        pending=pending,
    )
    result = loop.run("合并优化代码")
    handoff = "\n".join(item.content for item in loop.gateway.requests[-1].messages)
    assert "confirm_rewrite" in handoff
    assert "不要把全文写进 final" in handoff
    assert result.model_final == written
    assert result.finished is True
    assert pending.staged_items() == []
    landed = pending.items()
    assert [one.path for one in landed] == ["snake.html"]
    assert landed[0].new_text == new


def test_结论里的整份源码不会当成已经落盘(tmp_path: Path) -> None:
    """把文件贴进 final 只是一句话。不写 write_file，磁盘不会变。"""
    lines = "\n".join(f"<p>line {index}</p>" for index in range(40))
    pasted = f"合并后的代码如下：\n```html\n<html>\n{lines}\n</html>\n```"
    (tmp_path / "snake.html").write_text("<html>\nOLD\n</html>\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("贴源码", final=pasted),
            _turn("改口", final="还没写入文件。"),
        ],
        max_steps=4,
        supervise=False,
    )
    result = loop.run("合并优化代码")
    prompt = "\n".join(item.content for item in loop.gateway.requests[-1].messages)
    assert "不会落盘" in prompt
    assert "write_file" in prompt
    assert result.model_final == "还没写入文件。"
    assert "line 0" not in result.model_final
    assert (tmp_path / "snake.html").read_text(encoding="utf-8") == "<html>\nOLD\n</html>\n"
