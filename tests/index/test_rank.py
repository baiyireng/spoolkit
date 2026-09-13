from pathlib import Path

from spoolkit.index.indexer import index_project
from spoolkit.index.rank import (
    extract_keywords,
    prefetch,
    prefetch_contents,
    prefetch_scope,
    rank_files,
)
from spoolkit.index.tools import find_symbol_spec
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.store.db import init_schema, open_db
from spoolkit.tools.types import ToolCall


def _project(tmp_path: Path):
    (tmp_path / "parser.py").write_text(
        "def parse_config(path):\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "render.py").write_text(
        "def draw_screen():\n    pass\n", encoding="utf-8"
    )
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def test_关键词提取去掉英文停用词() -> None:
    words = extract_keywords("请帮我 fix the parse_config 函数")
    assert "parse_config" in words
    assert "the" not in words


def test_关键词提取去掉重复() -> None:
    assert extract_keywords("parse_config parse_config") == ["parse_config"]


def test_命中的文件排在前面(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    ranked = rank_files(conn, ["parse_config"])
    assert ranked[0] == "parser.py"
    conn.close()


def test_文件路径命中也能被选中(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert "render.py" in rank_files(conn, ["render"])
    conn.close()


def test_无命中时返回空列表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert rank_files(conn, ["完全不相关的词汇zzz"]) == []
    conn.close()


def test_预取结果包含相关文件符号表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = prefetch(conn, "修复 parse_config", OfflineTokenCounter(), 300)
    assert "parser.py" in text
    assert "def parse_config(path)" in text
    conn.close()


def test_预取遵守token上限(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"m{i}.py").write_text(
            "".join(f"def target_{j}():\n    pass\n" for j in range(20)),
            encoding="utf-8",
        )
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    text = prefetch(conn, "target", counter, 150)
    assert text
    assert counter.count(text) <= 150
    conn.close()


def test_无关键词时预取返回空串(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert prefetch(conn, "。。。", OfflineTokenCounter(), 300) == ""
    conn.close()


def test_内容预取给出文件正文(tmp_path: Path) -> None:
    """符号表只说「有什么」，正文才说「怎么写的」。

    实测本地 7B 只有符号表时会一直查、始终不读文件；给了正文它一轮就能改对。
    """
    conn = _project(tmp_path)
    text = prefetch_contents(
        conn, tmp_path, "修复 parse_config", OfflineTokenCounter()
    )
    assert "parser.py" in text
    assert "def parse_config(path):" in text
    assert "pass" in text
    conn.close()


def test_内容预取跳过过大的文件(tmp_path: Path) -> None:
    """大文件本来就该用 read_file 按行取，不能整个塞进上下文。"""
    (tmp_path / "huge.py").write_text(
        "def target():\n    pass\n" + "# 填充\n" * 4000, encoding="utf-8"
    )
    conn = _project(tmp_path)
    text = prefetch_contents(
        conn, tmp_path, "修复 target", OfflineTokenCounter(), max_files=1
    )
    assert text == ""
    conn.close()


def test_内容预取受总数上限约束(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    full = prefetch_contents(conn, tmp_path, "parse_config render", counter)
    tiny = prefetch_contents(
        conn, tmp_path, "parse_config render", counter, max_tokens=5
    )
    assert len(tiny) < len(full)
    conn.close()


def test_拿文件名当符号名查时给出改道提示(tmp_path: Path) -> None:
    """实测模型会这么做，然后收到「找不到符号」就卡住，连着三次。"""
    conn = _project(tmp_path)
    spec = find_symbol_spec(tmp_path, conn)
    result = spec.handler({"name": "parser.py"})
    assert result.ok is False
    assert "file_symbols" in result.content
    conn.close()


def test_普通符号找不到时不误导(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    spec = find_symbol_spec(tmp_path, conn)
    result = spec.handler({"name": "not_a_symbol"})
    assert result.ok is False
    assert "file_symbols" not in result.content
    conn.close()


def test_锚定预取给出范围文件的正文本(tmp_path: Path) -> None:
    """范围是「这一步要动哪儿」的权威信号，比关键词排序可靠。"""
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    text, spent, covered = prefetch_scope(
        tmp_path, ["parser.py"], counter, max_tokens=1000
    )
    assert "parser.py" in text
    assert "def parse_config(path):" in text
    assert covered == {"parser.py"}
    assert spent > 0
    conn.close()


def test_锚定预取列出同目录的条目(tmp_path: Path) -> None:
    """「旁边还有哪些文件」是修 import 这类问题唯一缺的信息。

    实测第 4 步 6 轮 12802 token：正文名额被两个测试文件占满，
    真正要改的 main.py 和它旁边的 helper.py 一个都没进来，
    于是它只能自己 list_dir + read_file 一路找。
    """
    (tmp_path / "helper.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    conn = _project(tmp_path)
    text, _, _ = prefetch_scope(
        tmp_path, ["parser.py"], OfflineTokenCounter(), max_tokens=1000
    )
    assert "render.py" in text or "helper.py" in text
    conn.close()


def test_锚定预取跳过通配范围(tmp_path: Path) -> None:
    """`**` 宽到没有信息量，锚定它等于把整个工作区塞进来。"""
    conn = _project(tmp_path)
    text, spent, covered = prefetch_scope(
        tmp_path, ["**", ""], OfflineTokenCounter(), max_tokens=1000
    )
    assert text == ""
    assert spent == 0
    assert covered == set()
    conn.close()


def test_锚定预取说明没放下的文件(tmp_path: Path) -> None:
    """放不下要说清楚。沉默会被读成「这个文件是空的」。"""
    big = tmp_path / "big.py"
    big.write_text("def target():\n    pass\n" + "# 填充\n" * 4000, encoding="utf-8")
    conn = _project(tmp_path)
    text, _, covered = prefetch_scope(
        tmp_path, ["big.py"], OfflineTokenCounter(), max_tokens=1000
    )
    assert "read_file" in text
    assert "big.py" in text
    assert covered == set()
    conn.close()


def test_内容预取不再重复锚定过的文件(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    text = prefetch_contents(
        conn,
        tmp_path,
        "修复 parse_config",
        counter,
        skip={"parser.py"},
    )
    assert "def parse_config(path):" not in text
    conn.close()


# 下面这条用的是**实测那一步的原文**（长任务第 4 步，修一个 import）：
# 步骤提示词里「已完成」列着别的题、「验收标准」写着 pytest、
# 契约里写着 `不得改动 test_04_fix_import.py`。
_真实步骤4提示词 = (
    "项目目标：这个工作区里放着 5 道编程题，每道题一个子目录。"
    "每道题目录里有 TASK.md、起始代码、以及一个 test_ 打头的验收测试"
    "（动手前先读它；也不要改它）。要求：把 5 道题都做对——"
    "每道题在它自己的目录里跑 python -m pytest -q 通过。\n"
    "已完成：修复 01_off_by_one 的 off-by-one 错误、修复 02_empty_input 空列表异常、"
    "在 03_add_function 新增 lower_first 函数\n"
    "涉及：04_fix_import/main.py\n"
    "本次只做这一步：修复 04_fix_import 的导入错误\n"
    "契约（不能动的接口与必须满足的断言）：导入名: from main import quadruple；"
    "调用: quadruple(n)；断言: quadruple(3)==12；不得改动 test_04_fix_import.py\n"
    "验收标准：在 04_fix_import/ 目录执行 python -m pytest -q，输出全绿（1 passed）\n"
    "不要顺手做后面步骤的事。"
)

_真实步骤4工作区 = {
    "04_fix_import/main.py": (
        "from helpers import double\n\n\ndef quadruple(x):\n"
        "    return double(double(x))\n"
    ),
    "04_fix_import/helper.py": "def double(x):\n    return x * 2\n",
    "04_fix_import/test_04_fix_import.py": (
        "from main import quadruple\n\n\ndef test_quadruple():\n"
        "    assert quadruple(3) == 12\n"
    ),
    "01_off_by_one/test_01_off_by_one.py": (
        "from calc import sum_to\n\n\ndef test_sum_to():\n    assert sum_to(5) == 15\n"
    ),
    "02_empty_input/test_02_empty_input.py": (
        "from stats import average\n\n\ndef test_empty():\n    assert average([]) == 0\n"
    ),
}


def test_关键词排序被步骤措辞带走时靠范围锚定补回来(tmp_path: Path) -> None:
    """这条讲的是第 4 步为什么贵：正文名额被两个**测试文件**占满。

    两个测试文件的名字都出现在提示词里（契约点名、已完成列表点名），
    于是它们各得 15 分上下，而真正要改的 `main.py` 只有 12 分——
    两个名额刚好全被它们拿走。执行者于是自己 list_dir + read_file 找，
    这一步 6 轮 12802 token。
    """
    for rel, body in _真实步骤4工作区.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    counter = OfflineTokenCounter()

    keywords = extract_keywords(_真实步骤4提示词)
    assert "04_fix_import/main.py" not in rank_files(conn, keywords, limit=2)

    text, _, covered = prefetch_scope(
        tmp_path, ["04_fix_import/main.py"], counter, max_tokens=1000
    )
    assert "from helpers import double" in text
    assert "helper.py" in text  # 同目录条目：修这个 import 靠的就是它
    assert covered == {"04_fix_import/main.py"}
    conn.close()
