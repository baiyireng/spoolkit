"""机器级小文件：坏了当空、写不进去要能看出来。"""
from pathlib import Path

import pytest

from spoolkit.kernel import jsonstore


def test_读不到就是默认值(tmp_path: Path) -> None:
    assert jsonstore.read_json(tmp_path / "没有这个.json", {"a": 1}) == {"a": 1}


def test_内容坏了也当默认值(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert jsonstore.read_json(path, {"a": 1}) == {"a": 1}


def test_改一次会原样落盘(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    jsonstore.update_json(path, lambda data: {**data, "n": data.get("n", 0) + 1}, {})
    jsonstore.update_json(path, lambda data: {**data, "n": data.get("n", 0) + 1}, {})
    assert jsonstore.read_json(path, {}) == {"n": 2}
    assert not path.with_name(path.name + ".tmp").exists()


def test_独占创建_文件已存在就报错且一个字节不改(tmp_path: Path) -> None:
    """`create_json` 是"我建，且只有我能建成"——撞上已有文件必须明确报错。

    这是同名会话并发创建那条竞态的地基：`write_json` 在这里会让**两个调用方
    都成功**（后写的赢），而没人知道发生过什么。
    """
    path = tmp_path / "meta.json"
    jsonstore.create_json(path, {"id": "a", "title": "赢家"})
    assert jsonstore.read_json(path, {}) == {"id": "a", "title": "赢家"}

    with pytest.raises(FileExistsError):
        jsonstore.create_json(path, {"id": "a", "title": "挑战者"})

    # 挑战者的内容一个字都没进去
    assert jsonstore.read_json(path, {})["title"] == "赢家"
    # 目录不存在也跟着建出来（调用方不该自己拼路径）
    deep = tmp_path / "x" / "y" / "meta.json"
    jsonstore.create_json(deep, {"ok": True})
    assert jsonstore.read_json(deep, {}) == {"ok": True}
