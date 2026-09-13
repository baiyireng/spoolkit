"""多语言符号提取：Python 之外也要有符号表。

只认 Python 的索引等于没有索引——一个 .js / .go / .rs 的工作区里符号表为空，
预取就没内容、查符号工具永远说"找不到"，而"找不到"会被读成"不存在"。

这一层是 best effort，所以测试盯的是**同一件事的两面**：认得出来的要认得，
认不准的宁可只报一行，也不能报一个错的区间（find_symbol 会把区间切给模型看）。
"""

from pathlib import Path

from agents_dev.index.indexer import index_project, iter_source_files
from agents_dev.index.symbols import (
    extractor_for,
    indexed_suffixes,
    language_name,
)
from agents_dev.index.tools import find_callers_spec
from agents_dev.store.db import init_schema, open_db


def _names(source: str, path: str) -> list[tuple[str, str]]:
    return [(s.name, s.kind) for s in extractor_for(path).extract(source, path)]


def test_JavaScript_认得函数与类() -> None:
    source = (
        "export function greet(name) {\n  return name;\n}\n"
        "class Box {\n  put(x) { return x; }\n}\n"
        "const add = (a, b) => a + b;\n"
        "const make = function () { return 1; };\n"
    )
    found = _names(source, "a.js")
    assert ("greet", "function") in found
    assert ("Box", "class") in found
    assert ("add", "function") in found
    assert ("make", "function") in found


def test_TypeScript_认得接口与类型别名() -> None:
    source = (
        "export interface Opt { a: number }\n"
        "export type ID = string;\n"
        "export class Api {\n  get() {}\n}\n"
    )
    found = dict(_names(source, "a.ts"))
    assert found["Opt"] == "interface"
    assert found["ID"] == "type"
    assert found["Api"] == "class"


def test_Go_的_type_声明认得出() -> None:
    """Go 写的是 `type Counter struct`，不是行首 struct。"""
    source = (
        "func Add(a int, b int) int {\n\treturn a + b\n}\n\n"
        "type Counter struct {\n\tn int\n}\n"
    )
    found = dict(_names(source, "a.go"))
    assert found["Add"] == "function"
    assert found["Counter"] == "struct"


def test_Rust_的_fn_与_struct() -> None:
    source = "pub fn run(x: u32) -> u32 {\n    x + 1\n}\n\nstruct Point { x: f64 }\n"
    found = dict(_names(source, "a.rs"))
    assert found["run"] == "function"
    assert found["Point"] == "struct"


def test_C_风格函数与_KR_风格都认得() -> None:
    source = (
        "static int add(int a, int b) {\n    return a + b;\n}\n\n"
        "int main(void)\n{\n    add(1, 2);\n}\n"
    )
    found = dict(_names(source, "a.c"))
    assert found["add"] == "function"
    assert found["main"] == "function"


def test_控制关键字不会被当成函数() -> None:
    """`if (x) {` 被索引成函数的话，符号表立刻变噪音。"""
    source = (
        "int main(void) {\n"
        "    if (add(1, 2)) { return 0; }\n"
        "    for (int i = 0; i < 3; i++) { }\n"
        "    while (x) { }\n"
        "}\n"
    )
    names = [name for name, _ in _names(source, "a.c")]
    assert names == ["main"]


def test_单行声明只报一行() -> None:
    """认不准就别硬算区间——find_symbol 会按区间切源码给模型看。"""
    source = "export type ID = string;\nexport class Api {\n  get() {}\n}\n"
    found = {s.name: (s.start_line, s.end_line) for s in extractor_for("a.ts").extract(source, "a.ts")}
    assert found["ID"] == (1, 1)
    assert found["Api"] == (2, 4)


def test_Ruby_的区间带上_end() -> None:
    source = "def greet(name)\n  puts name\nend\n"
    found = {s.name: (s.start_line, s.end_line) for s in extractor_for("a.rb").extract(source, "a.rb")}
    assert found["greet"] == (1, 3)


def test_注释行不当声明() -> None:
    source = "// function nope() {\n# def nope2()\n/* class Nope */\nfunction yes() {}\n"
    names = [name for name, _ in _names(source, "a.js")]
    assert names == ["yes"]


def test_后缀到语言的映射() -> None:
    assert language_name("a.py") == "python"
    assert language_name("a.JS") == "javascript"
    assert language_name("a.unknown") == "unknown"
    # 认不出的后缀也给提取器（通用扫描），而不是空实现
    assert extractor_for("a.unknown").extract("function f() {}\n", "a.unknown")


def test_索引按后缀挑提取器_并把语言记进文件表(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("function g() {}\n", encoding="utf-8")
    (tmp_path / "c.go").write_text("func h() {\n}\n", encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)

    stats = index_project(tmp_path, conn)

    assert stats.files_indexed == 3
    rows = {
        row["path"]: row["lang"]
        for row in conn.execute("SELECT path, lang FROM file").fetchall()
    }
    assert rows == {"a.py": "python", "b.js": "javascript", "c.go": "go"}
    names = {row["name"] for row in conn.execute("SELECT name FROM symbol").fetchall()}
    assert names == {"f", "g", "h"}
    conn.close()


def test_遍历会带上非_Python_文件(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("function g() {}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# 说明\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_text("x", encoding="utf-8")
    found = sorted(path.name for path in iter_source_files(tmp_path))
    assert found == ["README.md", "a.js"]
    assert ".js" in indexed_suffixes()


def test_代码文件先占索引名额(tmp_path: Path) -> None:
    """文件数上限是硬的：md/json 这类文本不该把代码挤出去。"""
    (tmp_path / "a.md").write_text("# 说明\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "c.json").write_text("{}\n", encoding="utf-8")
    found = [path.name for path in iter_source_files(tmp_path)]
    assert found[0] == "b.py"
    assert sorted(found) == ["a.md", "b.py", "c.json"]


def test_非Python符号的引用口径要说清楚(tmp_path: Path) -> None:
    """引用图只从 Python 的 AST 抽。别的语言上「没找到调用方」= 没查，
    不是没有——这两件事混起来，使用者会以为改这里安全。"""
    (tmp_path / "a.js").write_text("function greet() {}\n", encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)

    result = find_callers_spec(conn, root=tmp_path).handler({"name": "greet"})

    assert result.ok is True
    assert "引用图只对 Python 成立" in result.content
    conn.close()


def test_Python符号不加那段口径(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def greet():\n    pass\n", encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)

    result = find_callers_spec(conn, root=tmp_path).handler({"name": "greet"})

    assert result.ok is True
    assert "引用图只对 Python 成立" not in result.content
    conn.close()
