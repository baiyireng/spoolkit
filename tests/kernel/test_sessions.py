"""会话：有生命周期，才有"一个工作区多个会话"这件事。"""
from pathlib import Path

import pytest

from spoolkit.kernel import sessions


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_新工作区还没有会话(tmp_path: Path) -> None:
    assert sessions.list_sessions(_ws(tmp_path)) == []


def test_建一条就能列出来(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="修 calc.py")
    listed = sessions.list_sessions(root)
    assert [item.id for item in listed] == [made]
    assert listed[0].title == "修 calc.py"


def test_标题从第一句目标截出来(tmp_path: Path) -> None:
    assert sessions.title_from_goal("  ") == ""
    assert sessions.title_from_goal("修好 sum_to") == "修好 sum_to"
    long_goal = "把" + "很长的目标" * 20
    assert len(sessions.title_from_goal(long_goal)) == sessions.TITLE_LIMIT


def test_第一句目标填标题之后不再覆盖(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "第一句")
    sessions.ensure_title(root, made, "第二句")
    assert sessions.read_meta(root, made)["title"] == "第一句"


def test_改名(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="旧")
    sessions.rename_session(root, made, "新")
    assert sessions.list_sessions(root)[0].title == "新"


def test_归档只是从默认列表里藏起来(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="归档我")
    sessions.archive_session(root, made)
    assert sessions.list_sessions(root) == []
    assert [item.id for item in sessions.list_sessions(root, include_archived=True)] == [made]
    assert sessions.resolve_session(root, made) == made


def test_取消归档(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="来回")
    sessions.archive_session(root, made)
    sessions.archive_session(root, made, archived=False)
    assert [item.id for item in sessions.list_sessions(root)] == [made]


def test_按序号和latest解析(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    first = sessions.create_session(root, title="A")
    second = sessions.create_session(root, title="B")
    assert sessions.resolve_session(root, "latest") == second
    assert sessions.resolve_session(root, "1") == second  # 最近活跃在前
    assert sessions.resolve_session(root, "2") == first


def test_不认识的会话名要报错不要偷偷新建(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    first = sessions.create_session(root, title="A")
    with pytest.raises(sessions.SessionError) as raised:
        sessions.resolve_session(root, "查无此会话")
    assert first in str(raised.value)
    assert len(sessions.list_sessions(root)) == 1


def test_记忆库坏了也不炸(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / ".agent" / "memory.db").write_text("这不是 sqlite", encoding="utf-8")
    assert sessions.list_sessions(root) == []
