"""督导接进主循环之后：撞上限不再等于失败。

原先撞上步数上限就停，然后提示用户 `--resume` 再跑一次——那等于把系统的
判断成本推给用户，还把一件事拆成两次对话。现在由一个独立上下文的会话判断：
续期、改道、还是收手。这个文件测的是**接点**：谁在什么时候被叫起来、
它的话怎么进到模型面前、它坏了会怎么样。
"""

import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.registry import ToolRegistry


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


def _verdict(action: str, **rest) -> str:
    return json.dumps({"action": action, **rest}, ensure_ascii=False)


def _look() -> dict:
    return {"name": "list_dir", "arguments": {"path": "."}}


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
    # 督导是在漫游到第 8 步时被叫起来的，不是等到 20 步烧完。
    assert any("连续 8 步只看不写" in line for line in result.trace), result.trace


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
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert "自动验证已通过" in result.final
    assert any("督导判断已完成" in line for line in result.trace)


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
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert result.final == "8 个目录逐个跑 pytest 都通过，任务已完成。"
