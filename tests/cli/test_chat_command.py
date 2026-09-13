"""对话式使用：多轮、共用会话、历史只看不塞。

这条命令存在的理由是使用形状：真实使用更像聊天（先看一眼、再改一处、
再跑个测试），而 `run --goal` 每次都要把话说全。

有一条边界必须钉住：**聊天区域有历史（给人看），模型上下文没有历史**。
混为一谈会得出"把历史塞回上下文"的结论，而那会把这个项目的支点——
短上下文——直接推翻。
"""

import argparse
import json
from pathlib import Path

from spoolkit.cli.commands.chat import chat_command
from spoolkit.llm.fake import FakeModel


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _script(tmp_path: Path, answers: list[str]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return path


def _args(tmp_path: Path, script: Path, lines: list[str], **kwargs) -> argparse.Namespace:
    queue = list(lines)
    base = dict(
        root=str(tmp_path),
        provider="fake",
        model="",
        base_url="",
        proxy="",
        script=str(script),
        window=0,
        max_steps=2,
        step_ceiling=0,
        no_supervise=False,
        subagent_steps=2,
        no_memory=True,
        policy="auto",
        scope="**",
        session="chat",
        history=6,
        allow_read=[],
        goal="",
        reader=lambda: queue.pop(0) if queue else "",
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_两轮对话都跑到且共用同一个会话(tmp_path: Path, capsys) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = _script(tmp_path, [_turn("第一轮的答复"), _turn("第二轮的答复")])
    args = _args(tmp_path, script, ["看看 a.py", "再改一处", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "第一轮的答复" in out
    assert "第二轮的答复" in out
    assert "进入对话模式" in out


def test_exit_之后不再调用模型(tmp_path: Path, capsys) -> None:
    """脚本只给一轮答案：如果多跑了，假模型会报脚本耗尽。"""
    script = _script(tmp_path, [_turn("只有一轮")])
    args = _args(tmp_path, script, ["/exit"])

    assert chat_command(args) == 0
    assert "只有一轮" not in capsys.readouterr().out


def test_空行跳过_help_不调用模型(tmp_path: Path, capsys) -> None:
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["", "   ", "/help", "做点事", "/exit"])

    assert chat_command(args) == 0
    out = capsys.readouterr().out
    assert "/exit" in out  # help 文本打出来了
    assert "答复" in out


def test_开局打印状态行(tmp_path: Path, capsys) -> None:
    """开局那一眼：现在连的是哪儿、拿什么策略在跑。"""
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "工作区：" in out
    assert "供应商：fake" in out
    assert "授权：auto" in out


def test_config_与_policy_不调用模型(tmp_path: Path, capsys) -> None:
    """脚本只给一条应答：这两个命令要是真去跑模型，假模型会报脚本耗尽。"""
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/config", "/policy", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "配置文件：" in out
    assert "当前授权策略" in out


def test_会话里看得见配对请求也能就地批准(tmp_path: Path, capsys) -> None:
    """用户问过的场景：本机正开着会话，手机那边来了配对请求——我在会话里看得见吗？

    原先看不见（码只写在桥自己的终端上）。现在开局会提示，并且 `/approve <码>`
    就地放行；这条测试同时盯住"批完就不再提示"（否则每次进来都喊一遍）。
    """
    from spoolkit.bridge.pairing import Pairings

    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    code = Pairings(tmp_path / ".agent" / "bridge-pairings.json").ensure_code("openid-9")
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, [f"/approve {code}", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "待批准的通道配对请求" in out
    assert code in out
    assert "已批准 openid-9" in out
