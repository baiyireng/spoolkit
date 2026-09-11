from pathlib import Path

from agents_dev.tools.edit import (
    Baseline,
    BaselineEntry,
    PendingChanges,
    load_baseline,
    replace_lines_spec,
    revert,
    save_baseline,
    write_file_spec,
)


def _stage(tmp_path: Path) -> PendingChanges:
    pending = PendingChanges(tmp_path)
    (tmp_path / "keep.py").write_text("old = 1\n", encoding="utf-8")
    write_file_spec(tmp_path, pending).handler(
        {"path": "keep.py", "content": "new = 2\n"}
    )
    write_file_spec(tmp_path, pending).handler(
        {"path": "added.py", "content": "print(1)\n"}
    )
    return pending


def test_基线记录改动前内容(tmp_path: Path) -> None:
    baseline = _stage(tmp_path).baseline()
    entries = {e.path: e.old_text for e in baseline.entries}
    assert entries["keep.py"] == "old = 1\n"
    assert entries["added.py"] is None


def test_回滚恢复被改动的文件(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    baseline = pending.baseline()
    pending.apply()
    assert (tmp_path / "keep.py").read_text(encoding="utf-8") == "new = 2\n"

    revert(tmp_path, baseline)
    assert (tmp_path / "keep.py").read_text(encoding="utf-8") == "old = 1\n"


def test_回滚会删除新建的文件(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    baseline = pending.baseline()
    pending.apply()
    assert (tmp_path / "added.py").exists()

    revert(tmp_path, baseline)
    assert not (tmp_path / "added.py").exists()


def test_回滚返回被处理的路径(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    baseline = pending.baseline()
    pending.apply()
    assert sorted(revert(tmp_path, baseline)) == ["added.py", "keep.py"]


def test_基线可持久化并读回(tmp_path: Path) -> None:
    baseline = _stage(tmp_path).baseline()
    path = tmp_path / ".agent" / "last_change.json"
    save_baseline(path, baseline)
    assert load_baseline(path) == baseline


def test_基线文件不存在时返回空(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "nope.json") is None


def test_空基线回滚不产生副作用(tmp_path: Path) -> None:
    assert revert(tmp_path, Baseline(entries=())) == []


def test_按行替换的改动也能回滚(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 2, "content": "changed\n"}
    )
    baseline = pending.baseline()
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\nchanged\nl3\n"
    revert(tmp_path, baseline)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\nl2\nl3\n"


def test_回滚只影响基线里的文件(tmp_path: Path) -> None:
    (tmp_path / "untouched.py").write_text("safe = 1\n", encoding="utf-8")
    pending = _stage(tmp_path)
    baseline = pending.baseline()
    pending.apply()
    revert(tmp_path, baseline)
    assert (tmp_path / "untouched.py").read_text(encoding="utf-8") == "safe = 1\n"


def test_基线条目为纯数据可手工构造() -> None:
    entry = BaselineEntry(path="a.py", old_text="x")
    assert Baseline(entries=(entry,)).entries[0].path == "a.py"

