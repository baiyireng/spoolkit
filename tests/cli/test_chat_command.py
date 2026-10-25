"""对话式使用：多轮、共用会话。

真实使用更像聊天（先看一眼、再改一处、再跑个测试），而 `run --goal`
每次都要把话说全。每轮循环仍重置工具往返；最近几轮往来（截断）会带进
本轮提示，用来理解指代。
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


def test_无斜杠exit不进模型(tmp_path: Path, capsys) -> None:
    """`exit` / `QUIT` 不得当成目标跑一轮「再见」。"""
    for word in ("exit", "QUIT", "q"):
        args = _args(tmp_path, _script(tmp_path, [_turn("不该出现")]), [word])
        assert chat_command(args) == 0
        out = capsys.readouterr().out
        assert "不该出现" not in out, word


def test_回合中进度打到stderr(tmp_path: Path, capsys) -> None:
    """chat 绑 on_event：步骤可见，不只靠结束后的结论。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = _script(tmp_path, [_turn("结论正文")])
    args = _args(tmp_path, script, ["看看 a.py", "/exit"])
    assert chat_command(args) == 0
    captured = capsys.readouterr()
    assert "结论正文" in captured.out
    assert "第" in captured.err and "步" in captured.err


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


# --- 工作区与会话内核 · Task 5：对话里的工作区与会话 ---------------------------


def test_开局那一眼带上会话与来源(tmp_path: Path, capsys) -> None:
    """光说"在哪个工作区"不够：还要说清**为什么是它**（显式 / 向上找 / 指针 / 兜底）。"""
    from spoolkit.kernel import sessions

    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    sessions.create_session(tmp_path, title="修 calc.py")
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/exit"], session="")

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "会话：chat" in out  # 兜底名仍是 chat，老会话接得上
    assert "default" in out  # 来源要说出来
    assert "工作区：" in out


def test_斜杠命令看工作区与会话(tmp_path: Path, capsys) -> None:
    from spoolkit.kernel import workspaces as kws
    from spoolkit.kernel import sessions

    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    sessions.create_session(tmp_path, title="会话甲")
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/ws", "/sessions", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "登记过的工作区：" in out
    assert tmp_path.name in out
    assert "会话甲" in out


def test_new_开一条新会话并写指针(tmp_path: Path, capsys) -> None:
    from spoolkit.kernel import pointers, sessions

    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/new 修 calc.py", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    made = pointers.get("cli")
    assert made is not None
    assert made.workspace == tmp_path.resolve()
    assert made.session == sessions.list_sessions(tmp_path)[0].id
    assert "现在用会话" in out
    assert "修 calc.py" in out


def test_use_切到另一条会话并写指针(tmp_path: Path, capsys) -> None:
    from spoolkit.kernel import pointers, sessions

    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    first = sessions.create_session(tmp_path, title="甲")
    second = sessions.create_session(tmp_path, title="乙")
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, [f"/use {first}", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert pointers.get("cli").session == first
    assert f"现在用会话 {first}" in out
    assert second != first


def test_use_不认识的会话只报错不新建(tmp_path: Path, capsys) -> None:
    from spoolkit.kernel import sessions

    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    made = sessions.create_session(tmp_path, title="甲")
    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/use 查无此会话", "/exit"])

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "没有叫 查无此会话 的会话" in out
    assert len(sessions.list_sessions(tmp_path)) == 1
    assert made in out  # 候选里列得出来


def test_上次指向的工作区不在了就要说出来(tmp_path: Path, monkeypatch, capsys) -> None:
    """设计 §7：跳过不在了的指针、继续往下退，**但不静默换地方**。

    静默换地方的症状是"我明明指着 A，它却在 B 里干活"，属于最难查的那类问题。
    """
    import shutil

    from spoolkit.kernel import pointers, sessions
    from spoolkit.kernel import workspaces as kws

    gone = tmp_path / "gone"
    (gone / kws.MARK).mkdir(parents=True)
    home = tmp_path / "home"
    (home / kws.MARK).mkdir(parents=True)
    sessions.create_session(home, title="家里那条")
    kws.register(home)
    pointers.set("cli", gone, "cli")
    shutil.rmtree(gone)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)

    script = _script(tmp_path, [_turn("答复")])
    args = _args(tmp_path, script, ["/exit"], root=None, session="")

    assert chat_command(args) == 0

    out = capsys.readouterr().out
    assert "不在了" in out
    assert str(gone) in out
    assert f"工作区：{home}" in out  # 退到登记表里那个能用的
