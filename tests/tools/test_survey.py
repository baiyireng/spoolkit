"""「收齐这一处该看的」是一个动作，不是一个阶段。

实测它省的是步数：原先那条路要「列目录 → 猜文件名 → 读 → 猜错了换一个」，
模型确实猜错过（`read_file 03_add_function/word.py` 不存在），那一步白花。

这里盯三件事：目录里有什么要看得到；**测试优先给全文**（它是契约，
固定了模块对外的样子）；预算用完要明说哪些没给。
"""

from pathlib import Path

from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.survey import survey_spec
from spoolkit.tools.types import ToolCall


def _dir(tmp_path: Path) -> None:
    (tmp_path / "13_case").mkdir()
    home = tmp_path / "13_case"
    (home / "search.py").write_text(
        "def contains(text, needle):\n    return needle in text\n", encoding="utf-8"
    )
    (home / "test_13_case.py").write_text(
        "from search import contains\n\n\ndef test_it():\n"
        '    assert contains("Hello", "hello") is True\n',
        encoding="utf-8",
    )
    (home / "notes.md").write_text("随手记\n", encoding="utf-8")
    (home / "blob.bin").write_bytes(b"\x00\x01\x02")


def _spec(tmp_path: Path, pending=None):
    return survey_spec(tmp_path, OfflineTokenCounter(), pending)


def test_一次拿到目录_契约_代码(tmp_path: Path) -> None:
    _dir(tmp_path)
    result = _spec(tmp_path).handler({"path": "13_case"})
    assert result.ok
    text = result.content
    assert "test_13_case.py" in text
    # 契约给全文（它规定了模块对外长什么样）
    assert "assert contains" in text
    # 代码也给了
    assert "def contains" in text
    # 二进制不收
    assert "blob.bin" in text and "\x00" not in text


def test_测试排在代码前面(tmp_path: Path) -> None:
    """预算不够时先保契约——那三处「改坏了接口」的失败都是没看它。"""
    _dir(tmp_path)
    text = _spec(tmp_path).handler({"path": "13_case"}).content
    assert text.index("测试（契约）") < text.index("== 代码:")


def test_预算用完要明说哪些没给(tmp_path: Path) -> None:
    _dir(tmp_path)
    (tmp_path / "13_case" / "big.py").write_text(
        "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(60)),
        encoding="utf-8",
    )
    result = _spec(tmp_path).handler({"path": "13_case", "budget": 200})
    assert result.ok
    assert "没给" in result.content
    # 契约仍然先保住了
    assert "assert contains" in result.content


def test_看得见未落盘的改动(tmp_path: Path) -> None:
    """和别的读工具一致：刚写完的文件要读到新内容，而不是磁盘上的旧内容。"""
    _dir(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("13_case/search.py", "def contains(text, needle):\n    return True\n")
    text = _spec(tmp_path, pending).handler({"path": "13_case"}).content
    assert "return True" in text


def test_单个文件用read_file而不是它(tmp_path: Path) -> None:
    _dir(tmp_path)
    result = _spec(tmp_path).handler({"path": "13_case/search.py"})
    assert result.ok is False
    assert "不是目录" in result.content
