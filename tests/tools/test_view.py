"""读工具必须看到「改完之后」的世界。

没有这一层时模型活在两套矛盾的世界里：read_file 给旧内容，
run_command 在试跑副本里给新内容。实测审查者据此判定「改动没有落盘」，
把一处正确的实现判成了失败。
"""

from pathlib import Path

from agents_dev.index.indexer import index_project
from agents_dev.index.tools import file_symbols_spec, find_symbol_spec
from agents_dev.store.db import init_schema, open_db
from agents_dev.tools.edit import PendingChanges
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.search import search_code_spec


def _project(tmp_path: Path) -> PendingChanges:
    (tmp_path / "mod.py").write_text("def old_name():\n    return 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("KEEP = 1\n", encoding="utf-8")
    return PendingChanges(tmp_path)


def test_读到的是改动后的内容(tmp_path: Path) -> None:
    pending = _project(tmp_path)
    spec = read_file_spec(tmp_path, pending)
    assert "old_name" in spec.handler({"path": "mod.py"}).content

    pending.propose("mod.py", "def new_name():\n    return 2\n")
    assert "new_name" in spec.handler({"path": "mod.py"}).content
    assert "old_name" not in spec.handler({"path": "mod.py"}).content


def test_能读到还没落盘的新文件(tmp_path: Path) -> None:
    """磁盘上还不存在，但模型刚"新建"了它——它必须看得到。"""
    pending = _project(tmp_path)
    pending.propose("fresh.py", "VALUE = 9\n")
    result = read_file_spec(tmp_path, pending).handler({"path": "fresh.py"})
    assert result.ok is True
    assert "VALUE = 9" in result.content


def test_列目录能看到未落盘的新文件(tmp_path: Path) -> None:
    pending = _project(tmp_path)
    pending.propose("fresh.py", "VALUE = 9\n")
    content = list_dir_spec(tmp_path, pending).handler({"path": "."}).content
    assert "fresh.py" in content
    # 磁盘上没动过
    assert not (tmp_path / "fresh.py").exists()


def test_搜索看到改动后的内容(tmp_path: Path) -> None:
    pending = _project(tmp_path)
    pending.propose("mod.py", "def new_name():\n    return 2\n")
    spec = search_code_spec(tmp_path, pending)
    assert "new_name" in spec.handler({"pattern": "new_name"}).content
    # 改之前的名字不该再搜得到
    assert "old_name" not in spec.handler({"pattern": "old_name"}).content


def test_索引工具能看到改动后新增的符号(tmp_path: Path) -> None:
    """索引是按磁盘内容建的，新加的函数在索引里查不到。

    实测审查者因此报「找不到符号: clean_text」，进而判定改动没做。
    """
    pending = _project(tmp_path)
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    spec = find_symbol_spec(tmp_path, conn, pending)

    assert spec.handler({"name": "old_name"}).ok is True
    pending.propose("mod.py", "def new_name():\n    return 2\n")
    result = spec.handler({"name": "new_name"})
    assert result.ok is True, "改动里新增的符号要能查到"
    assert "mod.py" in result.content
    conn.close()


def test_文件符号表反映改动后的内容(tmp_path: Path) -> None:
    pending = _project(tmp_path)
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    spec = file_symbols_spec(conn, pending)

    assert "old_name" in spec.handler({"path": "mod.py"}).content
    pending.propose("mod.py", "def new_name():\n    return 2\n")
    content = spec.handler({"path": "mod.py"}).content
    assert "new_name" in content
    assert "old_name" not in content
    conn.close()


def test_没有改动时行为与以前一致(tmp_path: Path) -> None:
    """没接 pending 或没有改动时，读工具就该老老实实读磁盘。"""
    _project(tmp_path)
    assert "old_name" in read_file_spec(tmp_path).handler({"path": "mod.py"}).content
