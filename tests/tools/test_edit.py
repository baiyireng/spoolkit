from pathlib import Path

from agents_dev.tools.edit import (
    PendingChanges,
    make_diff,
    replace_lines_spec,
    replace_text_spec,
    write_file_spec,
)


def _pending(tmp_path: Path) -> PendingChanges:
    return PendingChanges(tmp_path)


def test_生成统一差异文本() -> None:
    diff = make_diff("a.py", "x = 1\n", "x = 2\n")
    assert "-x = 1" in diff
    assert "+x = 2" in diff
    assert "a/a.py" in diff


def test_写操作不落盘只产生diff(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "print(1)\n"}
    )
    assert result.ok is True
    assert "+print(1)" in result.content
    assert not (tmp_path / "new.py").exists()


def test_确认后写入磁盘(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "print(1)\n"}
    )
    written = pending.apply()
    assert written == ["new.py"]
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "print(1)\n"


def test_丢弃则不写入(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "print(1)\n"}
    )
    pending.discard()
    assert not (tmp_path / "new.py").exists()
    assert len(pending) == 0


def test_越界路径被拒绝(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "../outside.py", "content": "x\n"}
    )
    assert result.ok is False
    assert len(pending) == 0


def test_同一路径重复提出以最后一次为准(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    spec = write_file_spec(tmp_path, pending)
    spec.handler({"path": "a.py", "content": "第一版\n"})
    spec.handler({"path": "a.py", "content": "第二版\n"})
    assert len(pending) == 1
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "第二版\n"


def test_按行替换只改动指定区间(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 3, "content": "新行\n"}
    )
    assert result.ok is True
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\n新行\nl4\n"


def test_按行替换保留原有换行风格(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2", encoding="utf-8")
    pending = _pending(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 2, "content": "x"}
    )
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\nx"


def test_行号越界时拒绝(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 5, "end_line": 9, "content": "x"}
    )
    assert result.ok is False
    assert "行号区间非法" in result.content


def test_对不存在的文件按行替换会拒绝(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "nope.py", "start_line": 1, "end_line": 1, "content": "x"}
    )
    assert result.ok is False


def test_修改已有文件时diff显示原文与改后(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "new = 2\n"}
    )
    assert "-old = 1" in result.content
    assert "+new = 2" in result.content


def test_内容没变时不登记改动(tmp_path: Path) -> None:
    """空的 diff 也拿去问用户，等于教他一律点「应用」。"""
    (tmp_path / "a.py").write_text("same = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "same = 1\n"}
    )
    assert result.ok is True
    assert "没有产生改动" in result.content
    assert len(pending) == 0


def test_新建空文件仍然算改动(tmp_path: Path) -> None:
    """「内容为空」和「没有改动」是两回事，别把新建空文件吃掉。"""
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "empty.py", "content": ""}
    )
    assert result.ok is True
    assert len(pending) == 1
    pending.apply()
    assert (tmp_path / "empty.py").exists()


def test_改测试文件时会提醒(tmp_path: Path) -> None:
    """实测模型会把测试改成 assert True 然后宣布完成，白烧四步。"""
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "test_thing.py", "content": "def test_a():\n    assert True\n"}
    )
    assert result.ok is True
    assert "这是测试文件" in result.content


def test_改普通文件不提醒(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "thing.py", "content": "x = 1\n"}
    )
    assert "这是测试文件" not in result.content


def test_片段替换只改那一处(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def old_name(x):\n    return x\n", encoding="utf-8"
    )
    pending = _pending(tmp_path)
    result = replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "def old_name(x):", "new": "def new_name(x):"}
    )
    assert result.ok is True
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == (
        "def new_name(x):\n    return x\n"
    )


def test_片段找不到时拒绝(tmp_path: Path) -> None:
    """找不到多半是缩进或空格没对上，要让它回去看原文，而不是猜。"""
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "value = 2", "new": "value = 3"}
    )
    assert result.ok is False
    assert "找不到这段内容" in result.content
    assert len(pending) == 0


def test_片段不唯一时拒绝(tmp_path: Path) -> None:
    """不唯一就必须让它多带上下文——猜一处改错的代价比多问一句大得多。"""
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "x = 1", "new": "x = 2"}
    )
    assert result.ok is False
    assert "出现了 2 次" in result.content
    assert len(pending) == 0


def test_片段替换能删内容(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("keep\nremove\n", encoding="utf-8")
    pending = _pending(tmp_path)
    replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "remove\n", "new": ""}
    )
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "keep\n"
