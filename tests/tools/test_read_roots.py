"""额外可读根：读得到，写不到，工作区绑定不变。

为什么不直接把工作区绑到目标目录：那是把「看一个目录」和「换个项目」
混成一件事。工作区是身份——记忆、索引、检查点都挂在它上面——不该被一个
临时任务顺手换掉。
"""

from pathlib import Path

from spoolkit.agent.loop import build_workflow
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.search import search_code_spec


def _two_dirs(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "inside.py").write_text("INSIDE = 1\n", encoding="utf-8")
    (outside / "data.txt").write_text("外部内容\n", encoding="utf-8")
    return project, outside


def test_没授权时读不到(tmp_path: Path) -> None:
    project, outside = _two_dirs(tmp_path)
    result = read_file_spec(project).handler({"path": str(outside / "data.txt")})
    assert result.ok is False
    assert "越出可读范围" in result.content
    assert "--allow-read" in result.content, "要告诉人怎么授权"


def test_授权后读得到(tmp_path: Path) -> None:
    project, outside = _two_dirs(tmp_path)
    result = read_file_spec(project, None, (outside,)).handler(
        {"path": str(outside / "data.txt")}
    )
    assert result.ok is True
    assert "外部内容" in result.content


def test_授权后可列目录(tmp_path: Path) -> None:
    project, outside = _two_dirs(tmp_path)
    result = list_dir_spec(project, None, (outside,)).handler({"path": str(outside)})
    assert result.ok is True
    assert "data.txt" in result.content


def test_授权后可搜索(tmp_path: Path) -> None:
    project, outside = _two_dirs(tmp_path)
    result = search_code_spec(project, None, (outside,)).handler(
        {"pattern": "外部内容", "path": str(outside)}
    )
    assert result.ok is True
    assert "data.txt" in result.content


def test_工作区仍然读得到(tmp_path: Path) -> None:
    project, outside = _two_dirs(tmp_path)
    result = read_file_spec(project, None, (outside,)).handler({"path": "inside.py"})
    assert result.ok is True


def test_授权之外的目录仍然读不到(tmp_path: Path) -> None:
    """授权是逐目录的，不是「一旦开了就都能读」。"""
    project, outside = _two_dirs(tmp_path)
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    (forbidden / "secret.txt").write_text("不该读到\n", encoding="utf-8")
    result = read_file_spec(project, None, (outside,)).handler(
        {"path": str(forbidden / "secret.txt")}
    )
    assert result.ok is False


def test_写入仍然只在工作区内(tmp_path: Path) -> None:
    """只放开读——外面的目录不该能被写。"""
    from spoolkit.tools.edit import PendingChanges, write_file_spec

    project, outside = _two_dirs(tmp_path)
    pending = PendingChanges(project)
    result = write_file_spec(project, pending).handler(
        {"path": str(outside / "new.txt"), "content": "x\n"}
    )
    assert result.ok is False
    assert not (outside / "new.txt").exists()


def test_提示词里会说明可读范围(tmp_path: Path) -> None:
    project, outside = _two_dirs(tmp_path)
    text = build_workflow(ToolRegistry(), (outside,))
    assert str(outside) in text
    assert "只读" in text


def test_没有授权时不提这件事() -> None:
    text = build_workflow(ToolRegistry())
    assert "只读，不能写入" not in text
