"""本轮提示里的两件意图识别（Task 4）：一行指代提醒 + 有界摘要。

这一层是**真的会进模型**的那一处：`prompt_for_turn` 拼出来的字符串就是
`spool run` / `spool chat` 交给主循环的第一段话，所以断言钉在它身上，
而不是钉在某个纯函数上。两条硬要求：

- 提醒只在**命中指代词**时加（对所有消息都加 = 每轮付一笔没用的税）；
- 摘要/轮数/字符数都从标定表来（可用 `.agent/limits.json` 覆盖），
  函数里不许出现第二个写死的数。
"""

import argparse
import json
from pathlib import Path

from spoolkit import limits
from spoolkit.cli.runtime import prompt_for_turn
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.session import MemorySession
from spoolkit.memory.store import init_memory_schema, record_session
from spoolkit.memory.transcript import (
    ASSISTANT,
    DEICTIC_REMINDER,
    USER,
    record_message,
)
from spoolkit.store.db import open_db


def _memory(tmp_path: Path, session_id: str = "现在") -> MemorySession:
    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    record_session(conn, session_id, str(tmp_path), model="", context_limit=8192)
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / ".agent" / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
        session_id=session_id,
    )


def test_命中指代时加那一行_且只有一行(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    record_message(memory._conn, "现在", USER, "先看 python 那个脚本")
    text = prompt_for_turn(memory, "现在", "继续刚才那件事")
    # 钉「提醒就是那一行」而不是它的字面：措辞可以改，位置与条数不能变。
    assert text.startswith(DEICTIC_REMINDER)
    assert text.count(DEICTIC_REMINDER) == 1
    assert "当前：继续刚才那件事" in text
    memory._conn.close()


def test_指代提醒必须含住三件要点() -> None:
    """提醒是发给模型的一行规格：先检索再答、默认只查当前会话、没检索到不许猜。

    钉要点而不是钉整句：措辞一改，钉字面的断言就只会红在字面上，
    而它真正要守的是这三条语义。
    """
    assert "先调用 search_history" in DEICTIC_REMINDER
    assert "再作答" in DEICTIC_REMINDER
    assert "默认只查当前会话" in DEICTIC_REMINDER
    assert "未检索到" in DEICTIC_REMINDER and "不得推测" in DEICTIC_REMINDER


def test_普通消息一个字的提醒都不加(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    record_message(memory._conn, "现在", USER, "给 python 脚本加参数")
    text = prompt_for_turn(memory, "现在", "读一下 config.py")
    # 一个字都不加 = 整条提醒（任何一种措辞）都不出现。
    assert DEICTIC_REMINDER not in text
    assert "当前：读一下 config.py" in text
    memory._conn.close()


def test_没有记忆时提醒也不把原话挤掉(tmp_path: Path) -> None:
    text = prompt_for_turn(None, "现在", "还是按之前那样做")
    assert text.startswith(DEICTIC_REMINDER)
    assert "还是按之前那样做" in text
    assert prompt_for_turn(None, "现在", "读一下 config.py") == "读一下 config.py"


def test_摘要按标定值带进本轮提示(tmp_path: Path) -> None:
    """更早那一段的摘要自动进上下文：模型不必自己去检索。"""
    memory = _memory(tmp_path)
    record_message(memory._conn, "现在", USER, "给项目加个导出功能")
    record_message(memory._conn, "现在", ASSISTANT, "导出写完了，用了 csv 模块")
    # 中间再聊几轮，把最早那两句挤出"最近几轮"的窗口——它们只能靠摘要进来。
    for index in range(4):
        record_message(memory._conn, "现在", USER, f"中间第{index}问")
        record_message(memory._conn, "现在", ASSISTANT, f"中间第{index}答")
    record_message(memory._conn, "现在", USER, "读一下 config.py")
    text = prompt_for_turn(memory, "现在", "读一下 config.py", overrides=None)
    assert "更早的往来" in text
    assert "用了 csv 模块" in text
    memory._conn.close()


def test_标定值可覆盖_摘要能关掉(tmp_path: Path) -> None:
    """轮数/字符数是能力标定值：`.agent/limits.json` 覆盖得动，来源可查。"""
    memory = _memory(tmp_path)
    record_message(memory._conn, "现在", USER, "更早的一句话")
    record_message(memory._conn, "现在", USER, "读一下 config.py")
    text = prompt_for_turn(
        memory, "现在", "读一下 config.py",
        overrides={"history_digest_turns": 0, "recent_chat_turns": 1},
    )
    assert "更早的往来" not in text
    assert "当前：读一下 config.py" in text
    memory._conn.close()


def test_摘要的标定值登记在表里并能说出用处(tmp_path: Path) -> None:
    for name in ("history_digest_turns", "history_digest_chars"):
        entry = limits.knob(name)
        assert entry is not None and entry.overridable
        assert entry.note
    # 覆盖生效、来源变成「配置」，`spool limits` 因此能看出它是被改过的。
    root = tmp_path / "ws"
    root.mkdir()
    limits.save_overrides(root, {"history_digest_chars": 200})
    overrides = limits.load_overrides(root)
    value, source = limits.resolve("history_digest_chars", overrides)
    assert value == 200 and source == "配置"
    assert "history_digest_chars" in limits.render(overrides)


def test_摘要不把密钥带进去(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    record_message(
        memory._conn, "现在", USER, "配置是 SPOOLKIT_QQ_SECRET=abcdef123456 先放着"
    )
    record_message(memory._conn, "现在", USER, "读一下 config.py")
    text = prompt_for_turn(memory, "现在", "读一下 config.py")
    assert "abcdef123456" not in text
    assert "SPOOLKIT_QQ_SECRET" in text
    memory._conn.close()


# --- 真实的 `spool run` 那条路：默认作用域确实落在当前会话 -------------------


def _run_args(tmp_path: Path, script: Path, session: str, **kwargs) -> argparse.Namespace:
    base = dict(
        root=str(tmp_path),
        provider="fake",
        model="",
        base_url="",
        proxy="",
        script=str(script),
        window=4096,
        max_steps=2,
        step_ceiling=0,
        no_supervise=False,
        subagent_steps=2,
        no_memory=False,
        no_mcp=True,
        no_setup=True,
        delegate=False,
        plan=False,
        autonomous=False,
        resume=False,
        goal="归档那条命令为什么这么写",
        scope="**",
        cover="",
        limit=10,
        batch=2,
        no_roll=False,
        policy="auto",
        allow_read=[],
        events=False,
        from_channel=False,
        ask_on_stdin=False,
        web=False,
        web_allow=[],
        web_deny=[],
        web_search_api="",
        history=0,
        no_history=False,
        session=session,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _script(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _search_then_finish(query_args: dict) -> list[str]:
    return [
        json.dumps(
            {
                "thought": "先查历史",
                "tool_calls": [
                    {"name": "search_history", "arguments": query_args}
                ],
                "state": None,
                "done": False,
                "final": None,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "thought": "收尾",
                "tool_calls": [],
                "state": None,
                "done": True,
                "final": "好",
            },
            ensure_ascii=False,
        ),
    ]


def _run_with_gateway(tmp_path: Path, script: Path, session: str, monkeypatch):
    """跑真实的 `spool run`，并把那个假网关留下来看模型收到了什么。

    只看 stdout 不够：工具输出是**回灌给模型**的，不是打到屏幕上的。
    所以这里把 `provider_gateway` 包一层，把真正的网关记下来；
    脚本仍然走真实的装载路径（`--script` → FakeModel）。
    """
    from spoolkit.cli.commands import run as run_command

    real = run_command.provider_gateway
    seen: dict = {}

    def wrapper(args, project_root):
        gateway = real(args, project_root)
        seen["gateway"] = gateway
        return gateway

    monkeypatch.setattr(run_command, "provider_gateway", wrapper)
    args = _run_args(tmp_path, script, session=session)
    code = run_command.run(args)
    return code, seen.get("gateway")


def _tool_output(gateway) -> str:
    """第 2 次请求里回灌的内容——那就是模型看到的工具输出。"""
    assert len(gateway.requests) >= 2, "这一轮没有把工具输出回灌给模型"
    return "\n".join(m.content for m in gateway.requests[1].messages)


def _seed_two_sessions(tmp_path: Path) -> None:
    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    record_session(conn, "现在的会话", str(tmp_path), model="", context_limit=8192)
    record_session(conn, "上次排查", str(tmp_path), model="", context_limit=8192)
    # 关键词用库里那段的**独有词**：「目标」那句话本身也会出现在提示里，
    # 拿它判断"工具查到了什么"会误判。
    # 关键词取别的会话里的**独有词**：当前会话里没有它，默认检索必然为空，
    # 空结果那句话才会出现（有命中时它不该出现——这正是"只在需要时说"）。
    record_message(conn, "上次排查", USER, "归档那把锁加在写路径上")
    conn.close()


def test_run里不给session时只查当前会话(tmp_path: Path, monkeypatch) -> None:
    """端到端：模型不声明作用域时，别的会话里的话取不到，而且它被告知为什么。"""
    _seed_two_sessions(tmp_path)
    script = _script(tmp_path / "script.json", _search_then_finish({"query": "写路径"}))
    code, gateway = _run_with_gateway(tmp_path, script, "现在的会话", monkeypatch)
    assert code == 0
    back = _tool_output(gateway)
    # 别的会话**根本没有出现**：连出处都没有它的名字。
    assert "上次排查" not in back
    assert "那把锁加在写路径上" not in back
    assert "只查了当前会话" in back and 'session="*"' in back


def test_run里显式session_星号才跨会话(tmp_path: Path, monkeypatch) -> None:
    """同一条路，显式 `session="*"` 之后才看得到别的会话，且标出来源。"""
    _seed_two_sessions(tmp_path)
    script = _script(
        tmp_path / "script.json", _search_then_finish({"query": "写路径", "session": "*"})
    )
    code, gateway = _run_with_gateway(tmp_path, script, "现在的会话", monkeypatch)
    assert code == 0
    back = _tool_output(gateway)
    assert "那把锁加在写路径上" in back
    assert "会话 上次排查" in back  # 跨会话：出处里点名来源会话
    assert "来自多条会话" in back
