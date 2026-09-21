"""JS 语法检查验证器：从 HTML 抽出内联 JS 用 node --check 验证。

dogfood 暴露：贪吃蛇写完没有验证，step() 漏调 draw() 这种错误发现不了。
降级方案：至少拦住语法错（漏括号、未闭合字符串）。运行时逻辑错仍需浏览器。
"""
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.verify import (
    extract_js_from_html,
    make_js_verifier,
    make_verifier,
    _has_html_changes,
)


def test_抽出内联JS():
    html = """<html><body>
    <script>const x = 1;</script>
    <script src="ext.js"></script>
    <script>function f() { return x; }</script>
    </body></html>"""
    js = extract_js_from_html(html)
    assert "const x = 1;" in js
    assert "function f()" in js
    assert "ext.js" not in js  # 带 src 的跳过


def test_没有内联JS返回空串():
    html = '<html><script src="all.js"></script></html>'
    assert extract_js_from_html(html).strip() == ""


def test_检测到HTML改动(tmp_path: Path):
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "game.html", "content": "<script>var x = 1</script>"}
    )
    assert _has_html_changes(tmp_path, pending) is True


def test_没有HTML改动返回False(tmp_path: Path):
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "app.py", "content": "x = 1"}
    )
    assert _has_html_changes(tmp_path, pending) is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在 PATH")
def test_语法正确的JS通过检查(tmp_path: Path):
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "game.html", "content": "<script>const x = 1; function f() { return x; }</script>"}
    )
    verifier = make_js_verifier(tmp_path, pending)
    assert verifier is not None
    result = verifier()
    assert result.ok is True
    assert "语法检查通过" in result.content


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在 PATH")
def test_语法错误的JS被拦住(tmp_path: Path):
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "game.html", "content": "<script>const x = 1; function f( { return x; }</script>"}
    )
    verifier = make_js_verifier(tmp_path, pending)
    assert verifier is not None
    result = verifier()
    assert result.ok is False
    assert "语法错误" in result.content


def test_node不存在时返回None(tmp_path: Path):
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "game.html", "content": "<script>var x = 1</script>"}
    )
    with patch("spoolkit.tools.verify.shutil.which", return_value=None):
        assert make_js_verifier(tmp_path, pending) is None


def test_make_verifier对HTML文件fallback到JS检查(tmp_path: Path):
    """没有 pytest 但有 node 时，make_verifier 返回惰性 JS 验证器。
    装配时 pending 可以是空的——验证器每次调用时才检查 pending。"""
    pending = PendingChanges(tmp_path)
    with patch("spoolkit.tools.verify.shutil.which", return_value="/fake/node"):
        verifier = make_verifier(tmp_path, pending)
        assert verifier is not None  # node 在就返回惰性验证器
        # 装配时 pending 空，调用时返回「没有需要验证的改动」
        result = verifier()
        assert result.ok is True


def test_make_verifier惰性检查HTML改动(tmp_path: Path):
    """验证器装配时 pending 空，写完 HTML 后调用能检测到改动。"""
    pending = PendingChanges(tmp_path)
    with patch("spoolkit.tools.verify.shutil.which", return_value="/fake/node"):
        verifier = make_verifier(tmp_path, pending)
        assert verifier is not None
        # 装配后写 HTML 改动
        write_file_spec(tmp_path, pending).handler(
            {"path": "game.html", "content": "<script>var x = 1</script>"}
        )
        result = verifier()
        assert "HTML" in result.content or "JS" in result.content or "语法" in result.content
