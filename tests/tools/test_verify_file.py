"""按产出类型分派的验证入口（verify_file）与自动验证的那条链。

这一组用例要钉住的事只有一件：**验证能力必须和产出类型对齐**。
dogfood 现场是「要求写完必须验证，而验证器只认 JS 与 Python」——写 HTML 时
模型无处可验，只能反复撞 `node --check x.html`，9 步白费。

所以每个用例都拿**真文件**走一遍：绿的那份必须全绿（不许一律报错），
注入缺陷的那份必须报出「文件:行 + 错在哪」。
"""

import json
import shutil
from pathlib import Path

import pytest

from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.verify import (
    CANNOT_CHECK,
    NO_CHECKER,
    check_text,
    make_checker,
    make_js_verifier,
    make_verifier,
    verify_file_spec,
)

NODE = shutil.which("node") is not None
needs_node = pytest.mark.skipif(not NODE, reason="node 不在 PATH")

# 行号是这份夹具的一部分，每一条注入缺陷都按行断言——所以要改动它时一并改断言。
GOOD_HTML = """<!DOCTYPE html>
<html>
<head><title>贪吃蛇</title>
<style>
  body { color: #eee; }
</style>
</head>
<body>
<div id="cover">开始</div>
<canvas id="cv"></canvas>
<script>
const cv = document.getElementById('cv');
cv.width = 320;
</script>
</body>
</html>
"""


def _write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.write_text(text, encoding="utf-8")
    return target


def _verify(root: Path, name: str):
    return verify_file_spec(root).handler({"path": name})


def _inject(html: str, old: str, new: str) -> str:
    assert old in html, old
    return html.replace(old, new, 1)


# --- 绿：正确的那份必须真通过（否则这个检查器只会教模型忽略它） ----------


def test_正确的HTML通过并说清哪一步查不了(tmp_path: Path) -> None:
    _write(tmp_path, "game.html", GOOD_HTML)
    result = _verify(tmp_path, "game.html")
    assert result.ok is True, result.content
    assert "标签配对：通过" in result.content
    assert "id 引用：通过（声明 2 个，引用 1 处" in result.content
    assert "本地资源：没有引用本地文件" in result.content
    assert "内联 CSS：括号配对通过" in result.content
    # 有牙：通过了也必须说清「运行时行为没验」，不许沉默通过
    assert CANNOT_CHECK in result.content
    assert "在浏览器里打开看" in result.content


def test_正确的JSON与TOML与Python通过(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", json.dumps({"a": [1, 2]}, ensure_ascii=False))
    _write(tmp_path, "b.toml", '[project]\nname = "x"\n')
    _write(tmp_path, "c.py", "def f():\n    return 1\n")
    for name in ("a.json", "b.toml", "c.py"):
        result = _verify(tmp_path, name)
        assert result.ok is True, f"{name}: {result.content}"
        assert "通过" in result.content


# --- 红：五类注入缺陷（对应验收里那五条） --------------------------------


def test_引用了不存在的id被抓到带行号(tmp_path: Path) -> None:
    html = _inject(GOOD_HTML, "getElementById('cv')", "getElementById('ghost')")
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is False
    assert '引用了不存在的 id "ghost"' in result.content
    # 「文件:行」：位置要对回**原文件**，不是抽出来那段
    assert "game.html:12" in result.content


def test_重复的id被抓到带两处行号(tmp_path: Path) -> None:
    html = _inject(GOOD_HTML, '<canvas id="cv">', '<canvas id="cover">')
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is False
    assert 'id="cover" 重复' in result.content
    assert "game.html:9" in result.content
    assert "第 10 行" in result.content


def test_漏一个闭合标签被抓到(tmp_path: Path) -> None:
    # ① 漏了 </div>，后面还有别的结束标签：报「<div> 没有闭合，就先遇到了 …」
    html = _inject(GOOD_HTML, '<div id="cover">开始</div>', '<div id="cover">开始')
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is False
    assert "game.html:9：<div> 没有闭合" in result.content

    # ② 一直到文件结束都没闭合：报「一直没有闭合」
    _write(tmp_path, "tail.html", "<html><body><div>文字\n")
    tail = _verify(tmp_path, "tail.html")
    assert tail.ok is False
    assert "tail.html:1：<div> 一直没有闭合" in tail.content


def test_交叉嵌套被抓到(tmp_path: Path) -> None:
    html = "<html><body><div><span>文字</div></body></html>"
    _write(tmp_path, "bad.html", html)
    result = _verify(tmp_path, "bad.html")
    assert result.ok is False
    assert "<span> 没有闭合" in result.content


def test_两份文档拼在一起被抓到() -> None:
    """真实现场就是这样：两次生成的 HTML 首尾相接写进了同一个文件。"""
    text = GOOD_HTML + GOOD_HTML
    result = check_text(Path("."), "snake.html", text)
    assert result.ok is False
    assert "又一个 <html>" in result.content


def test_本地资源不存在被抓到_远程URL跳过(tmp_path: Path) -> None:
    html = _inject(
        GOOD_HTML,
        "<head><title>",
        '<head><script src="nope.js"></script>'
        '<link href="https://cdn.example.com/a.css" rel="stylesheet">'
        '<title>',
    )
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is False
    assert 'src="nope.js"' in result.content
    assert "指向的本地文件不存在" in result.content
    # 远程 URL 不联网也不能报成缺文件
    assert "cdn.example.com" not in result.content


def test_同目录下的本地资源存在就算过(tmp_path: Path) -> None:
    _write(tmp_path, "game.js", "const x = 1;\n")
    html = _inject(
        GOOD_HTML, "<head><title>", '<head><script src="game.js"></script><title>'
    )
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is True, result.content
    assert "本地资源：1 处引用都存在" in result.content


@needs_node
def test_内联JS语法错误给出文件与行号(tmp_path: Path) -> None:
    html = _inject(GOOD_HTML, "cv.width = 320;", "cv.width = ;")
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is False
    assert "JS 语法错误" in result.content
    # 行号必须对回 game.html 的第 13 行（抽出来的临时文件行号对模型没有意义）
    assert "game.html:13" in result.content
    assert "这一行是：cv.width = ;" in result.content


@needs_node
def test_内联JS语法通过时明确说只查语法(tmp_path: Path) -> None:
    _write(tmp_path, "game.html", GOOD_HTML)
    result = _verify(tmp_path, "game.html")
    assert "JS 语法检查通过" in result.content
    assert "运行时行为" in result.content  # 通过 ≠ 能用，这句话必须在


@needs_node
def test_js文件走node_check(tmp_path: Path) -> None:
    _write(tmp_path, "a.js", "function f( { return 1; }\n")
    result = _verify(tmp_path, "a.js")
    assert result.ok is False
    assert "a.js:1" in result.content
    assert "JS 语法错误" in result.content


def test_内联CSS括号不配对被抓到(tmp_path: Path) -> None:
    html = _inject(GOOD_HTML, "body { color: #eee; }", "body { color: #eee;")
    _write(tmp_path, "game.html", html)
    result = _verify(tmp_path, "game.html")
    assert result.ok is False
    assert "'{' 没有闭合" in result.content
    assert "game.html:5" in result.content


def test_未闭合注释被抓到(tmp_path: Path) -> None:
    _write(tmp_path, "a.css", "body { color: #eee; }\n/* 忘了关\n")
    result = _verify(tmp_path, "a.css")
    assert result.ok is False
    assert "注释没有闭合" in result.content
    assert "a.css:2" in result.content


def test_JSON解析失败带行列(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", '{\n  "a": 1,\n  "b": ,\n}\n')
    result = _verify(tmp_path, "a.json")
    assert result.ok is False
    assert "a.json:3" in result.content
    assert "JSON 解析失败" in result.content


def test_TOML解析失败带行号(tmp_path: Path) -> None:
    _write(tmp_path, "a.toml", '[project]\nname = "x"\n= 坏\n')
    result = _verify(tmp_path, "a.toml")
    assert result.ok is False
    assert "TOML 解析失败" in result.content


def test_Python语法错误带行号(tmp_path: Path) -> None:
    _write(tmp_path, "c.py", "def f(:\n    return 1\n")
    result = _verify(tmp_path, "c.py")
    assert result.ok is False
    assert "c.py:1" in result.content
    assert "Python 语法错误" in result.content


def test_有测试目录时提示可以跑pytest(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    _write(tmp_path, "c.py", "def f():\n    return 1\n")
    result = _verify(tmp_path, "c.py")
    assert result.ok is True
    assert "pytest" in result.content


# --- 其它类型：不许沉默通过，也不许把「查不了」说成「不合格」 ------------


def test_没有验证器的类型明说并给替代(tmp_path: Path) -> None:
    _write(tmp_path, "notes.md", "# 标题\n")
    result = _verify(tmp_path, "notes.md")
    assert NO_CHECKER in result.content
    assert "没有对应的验证器" in result.content
    assert "可行的替代" in result.content
    # 它不是缺陷，所以不能报成失败：否则每一步都往上下文里灌一句假失败
    assert result.ok is True


def test_自动验证里没有验证器的类型不产生反馈(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    pending.propose("notes.md", "# 标题\n")
    result = make_checker(tmp_path, pending)()
    assert result.ok is True
    assert result.content == ""


def test_未知扩展名也给替代(tmp_path: Path) -> None:
    _write(tmp_path, "run.ps1", "Write-Host 1\n")
    result = _verify(tmp_path, "run.ps1")
    assert NO_CHECKER in result.content
    assert "手动执行" in result.content


# --- 入口本身：一个工具，不是一堆并列工具 --------------------------------


def test_验证入口是一个按类型分派的工具(tmp_path: Path) -> None:
    spec = verify_file_spec(tmp_path)
    assert spec.name == "verify_file"
    assert spec.group == "核"
    assert list(spec.parameters["properties"]) == ["path"]
    assert spec.parameters["required"] == ["path"]
    # 描述里必须写清分派，否则模型不知道该拿它验什么
    for kind in (".html", ".js", ".json", ".toml", ".css"):
        assert kind in spec.description


def test_刚写完还没落盘的文件也能验(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "game.html", "content": _inject(GOOD_HTML, 'id="cv"', 'id="cover"')}
    )
    assert not (tmp_path / "game.html").exists()  # 只在待确认清单里
    result = verify_file_spec(tmp_path, pending).handler({"path": "game.html"})
    assert result.ok is False
    assert 'id="cover" 重复' in result.content


def test_文件不存在时给可行动的说明(tmp_path: Path) -> None:
    result = _verify(tmp_path, "nope.html")
    assert result.ok is False
    assert "文件不存在" in result.content
    assert "待确认的改动优先于磁盘" in result.content


# --- 自动验证那条链：写 HTML → 系统自己会验 -------------------------------


def _project_with_pytest(root: Path) -> None:
    """有 pytest 的项目——旧实现在这条路上**完全不验 HTML**，这正是漏洞。"""
    _write(root, "mod.py", "VALUE = 1\n")
    _write(root, "test_mod.py", "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 1\n")


def test_有pytest时自动验证也会验HTML(tmp_path: Path) -> None:
    """改回旧行为（HTML 只在没有 pytest 时降级验内联 JS）这条就是红的。

    旧实现里 make_verifier 一旦发现项目有 pytest 就只跑 pytest，HTML 的结构、
    id 引用、资源路径一个都不看——而「写 HTML 的项目里同时有测试」是最常见的形状。
    """
    _project_with_pytest(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("game.html", _inject(GOOD_HTML, "getElementById('cv')", "getElementById('ghost')"))
    verifier = make_verifier(tmp_path, pending)
    assert verifier is not None
    result = verifier(["game.html"])
    assert result.ok is False, result.content
    assert '引用了不存在的 id "ghost"' in result.content
    # pytest 那条路的措辞不能被破坏：测试确实跑过并报通过
    assert "1 passed" in result.content


def test_有pytest时HTML没问题就照旧绿(tmp_path: Path) -> None:
    _project_with_pytest(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("game.html", GOOD_HTML)
    result = make_verifier(tmp_path, pending)(["game.html"])
    assert result.ok is True, result.content


def test_没有pytest时自动验证走整套按类型(tmp_path: Path) -> None:
    """没有 pytest 的目录（真实现场就是这样的空工作区）：HTML 全查。"""
    pending = PendingChanges(tmp_path)
    pending.propose(
        "game.html", _inject(GOOD_HTML, '<div id="cover">开始</div>', '<div id="cover">开始')
    )
    verifier = make_verifier(tmp_path, pending)
    assert verifier is not None
    result = verifier()
    assert result.ok is False
    assert "<div> 没有闭合" in result.content


def test_没有pytest时坏JSON也会被自动验证抓到(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    pending.propose("a.json", '{"a": ,}\n')
    result = make_verifier(tmp_path, pending)()
    assert result.ok is False
    assert "JSON 解析失败" in result.content


def test_自动验证不回灌通过的噪声(tmp_path: Path) -> None:
    """每一步都灌一句「通过」是纯噪声；但「这一步查不了」必须留下。

    两种情况分开：通过的步骤（标签配对、id 引用…）在自动验证里不回灌；
    「HTML 的运行时行为查不了」会回灌——「语法通过」被当成「跑得起来」
    就是 dogfood 那次翻车，那句话是解药。
    """
    pending = PendingChanges(tmp_path)
    pending.propose("game.html", GOOD_HTML)
    result = make_verifier(tmp_path, pending)()
    assert result.ok is True  # 「查不了」不是缺陷，不能把绿的判成红的
    assert CANNOT_CHECK in result.content
    assert "标签配对：通过" not in result.content


def test_旧名字make_js_verifier给的是详细结论(tmp_path: Path) -> None:
    """历史入口保留：它给详细结论（自动验证那条路才做减法）。"""
    pending = PendingChanges(tmp_path)
    pending.propose("game.html", GOOD_HTML)
    verifier = make_js_verifier(tmp_path, pending)
    assert verifier is not None
    result = verifier()
    assert result.ok is True, result.content
    assert "标签配对：通过" in result.content
