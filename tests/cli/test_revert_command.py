from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.tools.edit import PendingChanges, save_baseline, write_file_spec


def _stage(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    write_file_spec(tmp_path, pending).handler({"path": "a.py", "content": "new\n"})
    save_baseline(tmp_path / ".agent" / "last_change.json", pending.baseline())
    pending.apply()


def test_回滚命令恢复文件(tmp_path: Path) -> None:
    _stage(tmp_path)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new\n"
    assert main(["revert", "--root", str(tmp_path)]) == 0
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old\n"


def test_回滚后基线被消费(tmp_path: Path) -> None:
    _stage(tmp_path)
    main(["revert", "--root", str(tmp_path)])
    assert not (tmp_path / ".agent" / "last_change.json").exists()


def test_没有基线时回滚返回非零(tmp_path: Path) -> None:
    assert main(["revert", "--root", str(tmp_path)]) == 2

