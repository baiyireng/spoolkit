from pathlib import Path

from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.hot import (
    DEMOTE_ORDER,
    HOT_TITLE,
    hot_budget,
    parse_hot,
    read_hot,
    render_hot,
    trim_to_budget,
    write_hot,
)


def test_渲染包含标题与分节() -> None:
    text = render_hot({"fact": ["测试命令是 pytest -q"], "doing": ["加增量更新"]})
    assert text.startswith(HOT_TITLE)
    assert "## 项目事实" in text
    assert "- 测试命令是 pytest -q" in text
    assert "## 进行中" in text


def test_空分节不渲染() -> None:
    text = render_hot({"fact": ["有内容"]})
    assert "## 用户偏好" not in text
    assert "## 教训" not in text


def test_渲染与解析可往返() -> None:
    sections = {
        "fact": ["事实一", "事实二"],
        "preference": ["偏好一"],
        "lesson": ["先上语法约束"],
    }
    assert parse_hot(render_hot(sections)) == sections


def test_解析空文本得到空分节() -> None:
    assert parse_hot("") == {}


def test_解析忽略未知分节() -> None:
    text = "# 项目记忆\n\n## 莫名其妙的标题\n- 内容\n\n## 项目事实\n- 事实\n"
    assert parse_hot(text) == {"fact": ["事实"]}


def test_容量按有效预算比例计算() -> None:
    assert hot_budget(8000) == int(8000 * 0.85 * 0.15)


def test_未超预算时不裁剪() -> None:
    sections = {"fact": ["短"]}
    kept, demoted = trim_to_budget(sections, OfflineTokenCounter(), 1000)
    assert kept == sections
    assert demoted == []


def test_超预算时按降级顺序先移除进行中() -> None:
    long_text = "很长的条目内容" * 20
    sections = {"fact": [long_text], "doing": [long_text]}
    kept, demoted = trim_to_budget(sections, OfflineTokenCounter(), 60)
    assert "doing" not in kept
    assert demoted


def test_降级顺序里事实最后被动() -> None:
    assert DEMOTE_ORDER[0] == "doing"
    assert DEMOTE_ORDER[-1] == "fact"


def test_同一分节内先移除最早的条目() -> None:
    sections = {"doing": ["第一条" * 20, "第二条" * 20]}
    kept, demoted = trim_to_budget(sections, OfflineTokenCounter(), 80)
    assert demoted[0].startswith("第一条")


def test_写入后可读回(tmp_path: Path) -> None:
    path = tmp_path / "memory.md"
    sections = {"fact": ["事实一"], "doing": ["进行中一"]}
    write_hot(path, sections)
    assert path.exists()
    assert read_hot(path) == sections


def test_读取不存在的文件返回空(tmp_path: Path) -> None:
    assert read_hot(tmp_path / "nope.md") == {}

