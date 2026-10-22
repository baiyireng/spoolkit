"""L1.1 补盲：**被改的文件自己**、**被删掉的文件**、**引用闭合**。

三个盲区都是实测出来的（设计稿 §1.7 G2、§2 的 L1.1/L1.2/L1.3）：

1. 旧口径要求名字在新正文里「以标识符形态彻底消失」，于是同一份文件里
   「定义改名了、调用点没改」这种**悬空引用**永远进不了候选——220652 那条
   探针的残留就落在被改的文件自己身上。
2. 删除走的是同一份台账，但旧代码一进门就 `if ... or change.deleted: return []`。
3. 改过的 `.py` 导入一个本仓并不存在的模块，以及被删掉的文件仍被
   `import` / 属性访问引用——这两条以前没人看。

判据全部是 `ast` + 文本扫描，**零误报是硬要求**：每条发现必须能指到
`文件:行`，指不到的宁可不报。P0 只报不阻断。
"""

from __future__ import annotations

from pathlib import Path

from spoolkit.tools.edit import (
    BROKEN_IMPORT_KIND,
    DELETED_REFERENCE_KIND,
    STALE_REFERENCE_KIND,
    PendingChange,
    PendingChanges,
    broken_local_imports,
    lingering_hits,
    lingering_note,
)


def _pending(root: Path) -> PendingChanges:
    return PendingChanges(root)


def _modified(root: Path, path: str, old: str, new: str) -> PendingChanges:
    """登记一处「改内容」的改动，并把新正文写到磁盘（视图与磁盘保持一致）。"""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new, encoding="utf-8")
    pending = _pending(root)
    pending._changes[path] = PendingChange(path=path, old_text=old, new_text=new)
    return pending


def _deleted(root: Path, path: str, old: str) -> PendingChanges:
    """登记一处删除。磁盘上文件**还在**（改动没落盘），台账里已经删了。"""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(old, encoding="utf-8")
    pending = _pending(root)
    pending._changes[path] = PendingChange(
        path=path, old_text=old, new_text="", deleted=True
    )
    return pending


# ---- 盲区 1：被改的文件自己 --------------------------------------------------


OLD_FETCH = (
    "def get_rows(source):\n"
    "    return []\n"
    "\n"
    "\n"
    "def render(source):\n"
    "    return get_rows(source)\n"
)


def test_本文件内的悬空引用会被报出来(tmp_path: Path) -> None:
    """定义改名了、同一份文件里的调用点没改：旧名还在**这个文件里**。"""
    new = (
        "def load_rows(source):\n"
        "    return []\n"
        "\n"
        "\n"
        "def render(source):\n"
        "    return get_rows(source)\n"
    )
    pending = _modified(tmp_path, "pipeline/fetch.py", OLD_FETCH, new)
    change = pending.items()[0]

    hits = lingering_hits(tmp_path, pending, change)

    assert [hit.kind for hit in hits] == [STALE_REFERENCE_KIND]
    # 判据是全机械的：文件、行号、名字三件都指得出来。
    assert hits[0].path == "pipeline/fetch.py"
    assert hits[0].line == 6, "第 6 行还写着 get_rows"
    assert hits[0].name == "get_rows"
    assert "本文件内" in hits[0].detail
    # 报告也要把这一条说成「本文件内」，而不是「别的文件」。
    note = lingering_note(tmp_path, pending, change)
    assert "pipeline/fetch.py 第 6 行" in note


def test_兼容别名不算残留(tmp_path: Path) -> None:
    """`get_rows = load_rows` 是一次**绑定**，不是悬空引用（实测这条误报的代价：
    模型掉头去删自己刚留的别名，连着 4 步打转、19 步红）。"""
    new = (
        "def load_rows(source):\n"
        "    return []\n"
        "\n"
        "\n"
        "# 兼容旧名\n"
        "get_rows = load_rows\n"
    )
    pending = _modified(tmp_path, "pkg/fetch.py", OLD_FETCH, new)

    hits = lingering_hits(tmp_path, pending, pending.items()[0])

    assert hits == []
    assert lingering_note(tmp_path, pending, pending.items()[0]) == ""


def test_注释与文档串里提到旧名不算悬空引用(tmp_path: Path) -> None:
    """`ast` 只认**读用处**：注释/文档串里提到旧名不是代码引用，不报。"""
    new = (
        '"""这个模块原来的 get_rows 已经改名。"""\n'
        "\n"
        "\n"
        "def load_rows(source):\n"
        "    return []\n"
    )
    pending = _modified(tmp_path, "pkg/fetch.py", OLD_FETCH, new)

    assert lingering_hits(tmp_path, pending, pending.items()[0]) == []


def test_没改名时什么都不报(tmp_path: Path) -> None:
    """普通改写不该被扣上「改名」的帽子。"""
    old = "def total(rows):\n    return 0\n"
    new = "def total(rows):\n    return 1\n"
    pending = _modified(tmp_path, "store.py", old, new)

    assert lingering_hits(tmp_path, pending, pending.items()[0]) == []


# ---- 盲区 2：被删掉的文件 ----------------------------------------------------


def test_删掉的文件仍被按名字提到会被报出来(tmp_path: Path) -> None:
    pending = _deleted(tmp_path, "pipeline/fetch.py", OLD_FETCH)
    (tmp_path / "README.md").write_text("入口是 `get_rows`。\n", encoding="utf-8")

    hits = lingering_hits(tmp_path, pending, pending.items()[0])

    assert hits and hits[0].kind == DELETED_REFERENCE_KIND
    assert hits[0].path == "README.md"
    assert hits[0].line == 1
    assert "已被删除" in hits[0].detail


def test_删掉的文件仍被_import_会被报出来(tmp_path: Path) -> None:
    pending = _deleted(tmp_path, "pipeline/fetch.py", OLD_FETCH)
    (tmp_path / "pipeline" / "report.py").write_text(
        "from pipeline.fetch import get_rows\n", encoding="utf-8"
    )

    hits = lingering_hits(tmp_path, pending, pending.items()[0])

    assert [(hit.path, hit.line) for hit in hits] == [("pipeline/report.py", 1)]
    assert "import" in hits[0].detail


def test_删掉的文件仍被相对导入与属性访问引用会被报出来(tmp_path: Path) -> None:
    """`from .fetch import x` 与 `pipeline.fetch.rows()` 两种形状都要看得见。"""
    pending = _deleted(tmp_path, "pipeline/fetch.py", OLD_FETCH)
    (tmp_path / "pipeline" / "cli.py").write_text(
        "import pipeline\n\n\ndef main():\n    return pipeline.fetch.rows()\n",
        encoding="utf-8",
    )
    (tmp_path / "pipeline" / "report.py").write_text(
        "from .fetch import get_rows\n", encoding="utf-8"
    )

    hits = lingering_hits(tmp_path, pending, pending.items()[0])

    assert {(hit.path, hit.line) for hit in hits} == {
        ("pipeline/report.py", 1),
        ("pipeline/cli.py", 5),
    }


def test_py_里的字符串提到已删文件不算残留(tmp_path: Path) -> None:
    """`assert not Path("pipeline/fetch.py").exists()` 正是「确认它删掉了」的
    写法——报它就是把对的判成错的（零误报是硬要求）。"""
    pending = _deleted(tmp_path, "pipeline/fetch.py", OLD_FETCH)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_gone.py").write_text(
        'from pathlib import Path\n\n\ndef test_gone():\n'
        '    assert not Path("pipeline/fetch.py").exists()\n',
        encoding="utf-8",
    )

    assert lingering_hits(tmp_path, pending, pending.items()[0]) == []


# ---- 盲区 3：引用闭合 --------------------------------------------------------


def _written(root: Path, path: str, text: str) -> PendingChanges:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    pending = _pending(root)
    pending._changes[path] = PendingChange(path=path, old_text="", new_text=text)
    return pending


def test_导入本仓不存在的本地模块会被报出来(tmp_path: Path) -> None:
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    pending = _written(
        tmp_path,
        "pipeline/report.py",
        "from pipeline.missing import load_rows\n",
    )

    hits = broken_local_imports(tmp_path, pending, ["pipeline/report.py"])

    assert [hit.kind for hit in hits] == [BROKEN_IMPORT_KIND]
    assert hits[0].path == "pipeline/report.py"
    assert hits[0].line == 1
    assert "pipeline.missing" in hits[0].detail


def test_标准库与第三方导入不判(tmp_path: Path) -> None:
    """只判「第一方」：第三方装在哪儿不归工作区管（§7 不硬造判据）。"""
    pending = _written(
        tmp_path,
        "pipeline/report.py",
        "import json\nimport os.path\nfrom typing import Any\nfrom requests import get\n",
    )

    assert broken_local_imports(tmp_path, pending, ["pipeline/report.py"]) == []


def test_本地模块存在时不报(tmp_path: Path) -> None:
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pipeline" / "fetch.py").write_text("def load_rows():\n    return []\n", encoding="utf-8")
    pending = _written(
        tmp_path,
        "pipeline/report.py",
        "from pipeline.fetch import load_rows\n",
    )

    assert broken_local_imports(tmp_path, pending, ["pipeline/report.py"]) == []


# ---- 话术里的反向条款（§2.5 的硬规矩）---------------------------------------


def test_残留话术带反向条款且不许说已统一(tmp_path: Path) -> None:
    pending = _modified(
        tmp_path,
        "pipeline/fetch.py",
        OLD_FETCH,
        "def load_rows(source):\n    return []\n\n\ndef render(source):\n    return get_rows(source)\n",
    )
    note = lingering_note(tmp_path, pending, pending.items()[0])

    assert "不要说「整个项目里都统一了」" in note
    # 反向条款必须一起进：只留「去把引用改对」那半段会被读成「禁止删除」。
    assert "删除已被替代的文件" in note
    assert "允许的正常编辑" in note
    assert "禁止删除" not in note
