from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.kernel import pointers, sessions
from spoolkit.memory.store import init_memory_schema, record_session
from spoolkit.memory.transcript import ASSISTANT, USER, record_message
from spoolkit.store.db import open_db


def _seed(tmp_path: Path, *names: str) -> None:
    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    for index, name in enumerate(names):
        record_session(
            conn, name, str(tmp_path), model=f"gemini:m{index}", context_limit=8192
        )
    conn.close()


def test_列出会话(tmp_path: Path, capsys) -> None:
    _seed(tmp_path, "morning", "bugfix")
    assert main(["session", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "morning" in out
    assert "bugfix" in out
    assert "共 2 个会话" in out


def test_没有数据库时明确报错(tmp_path: Path, capsys) -> None:
    assert main(["session", "--root", str(tmp_path)]) == 2
    assert "还没有任何会话" in capsys.readouterr().err


def test_有库但没有会话时提示(tmp_path: Path, capsys) -> None:
    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    conn.close()
    assert main(["session", "--root", str(tmp_path)]) == 2
    assert "还没有会话记录" in capsys.readouterr().err


def test_显示模型与窗口(tmp_path: Path, capsys) -> None:
    _seed(tmp_path, "work")
    main(["session", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "gemini:m0" in out
    assert "8192" in out


# --- 会话生命周期（工作区与会话内核 · Task 5）---


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_建一条并写指针(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    monkeypatch.chdir(root)
    assert main(["session", "new", "修 calc.py"]) == 0
    got = pointers.get("cli")
    assert got is not None and got.session != ""
    assert sessions.list_sessions(root)[0].title == "修 calc.py"


def test_切到另一条会话(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    monkeypatch.chdir(root)
    first = sessions.create_session(root, title="A")
    second = sessions.create_session(root, title="B")
    assert main(["session", "use", first]) == 0
    assert pointers.get("cli").session == first
    assert main(["session", "use", "latest"]) == 0
    assert pointers.get("cli").session == second


def test_改名与归档(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    monkeypatch.chdir(root)
    made = sessions.create_session(root, title="旧名")
    assert main(["session", "rename", made, "新名"]) == 0
    assert sessions.list_sessions(root)[0].title == "新名"
    assert main(["session", "archive", made]) == 0
    assert sessions.list_sessions(root) == []


# --- 检索历史消息（`search_history` 那条路的命令行入口）---


def _messages(root: Path, *rows: tuple[str, str, str]) -> None:
    """写几条聊天往来，并把涉及的会话登记进库。

    登记这一步不能省：`session search` 的默认作用域是**当前会话**，而"当前会话"
    是拿 `list_sessions` 解出来的——只在 transcript 里塞行、不登记会话，等于
    库里有一条不认识的历史，`--limit` 那类参数错误也就走不到该走的分支。
    """
    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    seen = set()
    for session_id, role, content in rows:
        if session_id not in seen:
            seen.add(session_id)
            record_session(conn, session_id, str(root), model="", context_limit=8192)
        record_message(conn, session_id, role, content)
    conn.close()


def _current(root: Path, session_id: str) -> None:
    """把「当前会话」定成某一条：`session search` 默认只查它。"""
    pointers.set("cli", root, session_id)


def test_默认只查当前会话(tmp_path: Path, capsys) -> None:
    """契约变更：默认作用域从「本工作区全部会话」收到**当前会话**。"""
    root = _ws(tmp_path)
    _messages(
        root,
        ("上次排查", USER, "归档那条命令为什么这么写"),
        ("现在", USER, "归档在这条会话里说过"),
    )
    _current(root, "现在")
    assert main(["session", "--root", str(root), "search", "归档"]) == 0
    out = capsys.readouterr().out
    assert "现在" in out and "当前会话 现在" in out
    assert "上次排查" not in out


def test_跨会话要显式给_all_sessions(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path)
    _messages(
        root,
        ("上次排查", USER, "归档那条命令为什么这么写"),
        ("现在", ASSISTANT, "接着说"),
    )
    _current(root, "现在")
    assert (
        main(["session", "--root", str(root), "search", "归档", "--all-sessions"]) == 0
    )
    out = capsys.readouterr().out
    assert "上次排查" in out
    assert "命中第" in out
    assert "接着说" not in out
    # 跨会话时抬头要说清范围与「来自多条会话」。
    assert "本工作区的全部会话" in out
    assert "来自多条会话" in out


def test_点名一条会话(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path)
    _messages(
        root,
        ("上次排查", USER, "归档要加锁"),
        ("现在", USER, "归档已经加锁了"),
    )
    _current(root, "现在")
    assert (
        main(
            [
                "session", "--root", str(root), "search", "归档",
                "--from-session", "上次排查",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "会话 上次排查" in out
    assert "已经加锁" not in out


def test_检索按标题给出处(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path)
    sessions.write_meta(root, "alpha", title="修 calc.py", created_at=1.0)
    _messages(root, ("alpha", USER, "归档要加锁"))
    _current(root, "alpha")
    assert main(["session", "--root", str(root), "search", "归档"]) == 0
    assert "修 calc.py" in capsys.readouterr().out


def test_检索没命中时给退出码1并说清范围(tmp_path: Path, capsys) -> None:
    """"搜到没有"要能和"用法错"分开，脚本里得能直接判。"""
    root = _ws(tmp_path)
    _messages(root, ("现在", USER, "无关的话"))
    _current(root, "现在")
    assert main(["session", "--root", str(root), "search", "完全无关"]) == 1
    out = capsys.readouterr().out
    assert "没有找到" in out
    # 空结果必须说清两件事：默认只查了当前会话、跨会话怎么显式要。
    assert "当前会话 现在" in out
    assert "只查了当前会话" in out and "--all-sessions" in out


def test_没给关键词时给退出码2(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path)
    assert main(["session", "--root", str(root), "search"]) == 2
    assert "关键词" in capsys.readouterr().err


def test_检索的非法参数都报错(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path)
    _messages(root, ("s", USER, "归档要加锁"))
    # `--limit 0` 是"没给"（argparse 默认值），越界的是负数与超出上限。
    assert main(["session", "--root", str(root), "search", "归档", "--limit", "-1"]) == 2
    assert main(["session", "--root", str(root), "search", "归档", "--limit", "999"]) == 2
    assert main(["session", "--root", str(root), "search", "归档", "--since", "前天"]) == 2
    err = capsys.readouterr().err
    assert "limit" in err and "前天" in err


def test_没有记忆库时明确报错(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path)
    assert main(["session", "--root", str(root), "search", "归档"]) == 2
    assert "还没有任何会话" in capsys.readouterr().err

