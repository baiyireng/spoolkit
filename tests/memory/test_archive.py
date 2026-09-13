from pathlib import Path

from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.archive import archive_task
from spoolkit.memory.hot import read_hot, write_hot
from spoolkit.memory.store import init_memory_schema, list_memories
from spoolkit.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py", "确认索引表结构"],
        current="完成",
        excluded=["整文件重解析（太慢）"],
        step=4,
    )


def test_归档写入一条事件(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    assert result.episode_id > 0
    rows = conn.execute("SELECT task, outcome FROM episode").fetchall()
    assert rows[0]["task"] == "给 parser 加增量更新"
    assert rows[0]["outcome"] == "success"
    conn.close()


def test_已排除的方案写成失败记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    failures = list_memories(conn, kind="failure")
    assert len(failures) == 1
    assert "整文件重解析" in failures[0].text
    assert failures[0].source.startswith("ep#")
    conn.close()


def test_任务结束后进行中条目被清空(tmp_path: Path) -> None:
    hot_path = tmp_path / "memory.md"
    write_hot(hot_path, {"doing": ["给 parser 加增量更新"], "fact": ["事实一"]})
    conn = _conn(tmp_path)
    archive_task(
        conn,
        hot_path,
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    assert "doing" not in read_hot(hot_path)
    assert read_hot(hot_path)["fact"] == ["事实一"]
    conn.close()


def test_提升候选写入热记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "测试命令是 pytest -q")],
    )
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    conn.close()


def test_超出预算的条目下沉到冷记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    long_entry = "很长的一段记忆内容" * 40
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=2000,
        promote=[("doing", long_entry), ("fact", "短事实")],
    )
    assert result.demoted
    assert any("很长的一段记忆内容" in m.text for m in list_memories(conn, kind="episode"))
    conn.close()


def test_教训被路由到教训库而不是热记忆文件(tmp_path: Path) -> None:
    from spoolkit.memory.lessons import match_lessons

    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("lesson", "解析自定义格式先上语法约束", "解析,正则,格式")],
    )
    # 不进热记忆文件
    assert "lesson" not in read_hot(tmp_path / "memory.md")
    # 但能被场景触发
    assert len(match_lessons(conn, "修一下解析逻辑")) == 1
    conn.close()


def test_教训保留来源便于回溯(tmp_path: Path) -> None:
    from spoolkit.memory.lessons import match_lessons

    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("lesson", "规则", "解析")],
    )
    hit = match_lessons(conn, "解析")[0]
    assert hit.source.startswith("ep#")
    conn.close()


def test_非教训条目仍然进热记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "测试命令是 pytest -q")],
    )
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    conn.close()


def test_兼容不带触发词的两元组(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "旧格式的条目")],
    )
    assert result.promoted == ["旧格式的条目"]
    conn.close()


def test_归档结果报告提升与下沉(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "事实一")],
    )
    assert result.promoted == ["事实一"]
    assert result.demoted == []
    conn.close()


def test_失败任务的事件outcome为失败(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        outcome="fail",
    )
    assert conn.execute("SELECT outcome FROM episode").fetchone()["outcome"] == "fail"
    conn.close()
