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
