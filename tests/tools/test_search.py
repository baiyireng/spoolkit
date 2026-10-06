"""`search_code` 的行为。

除了「命中/不命中/不逃出根」这三条，这里还钉住一条**实测过的缺陷**：

本机沙箱里 `mkdir(mode=0o700)` 建出来的目录（pytest 缓存、`pytest-cache-files-*`）
自己就失去了访问权，`rg` 走到那里会打印
`rg: …\\pytest-cache-files-xxx: 拒绝访问。 (os error 5)` 并以 **2** 退出。
改前：退出码不是 0/1 一律判「搜索失败」，于是**能读的部分搜到了也全被丢掉**，
模型只好换路重试（实测多走 3 步、督导介入从 2 涨到 7）。

改后：跳过读不了的路径、把「跳过了什么」写在结果里（**不许静默**），
只有**别的**错误（比如正则写错）才算失败；起点自己就读不了时给人话，
不拿空结果冒充成功。
"""

import subprocess
from pathlib import Path

import pytest

from spoolkit.tools.search import search_code_spec

REPO_ROOT = Path(__file__).resolve().parents[2]


def _completed(stdout: str, stderr: str, returncode: int = 2):
    """假装是 rg 跑完的样子（不真的起进程：本机有没有 rg 不该决定这些用例）。"""
    return subprocess.CompletedProcess(
        args=["rg"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_run(monkeypatch, result):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)


def _locked_dirs():
    """本机真的读不了的目录（没有就返回空表，那条用例自己跳过）。"""
    try:
        children = list(REPO_ROOT.iterdir())
    except OSError:  # pragma: no cover - 仓库根都读不了就没法测了
        return []
    found = []
    for child in sorted(children):
        if not child.is_dir():
            continue
        try:
            list(child.iterdir())
        except OSError:
            found.append(child)
    return found


def test_搜索命中并返回文件名(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "def target"})
    assert result.ok is True
    assert "a.py" in result.content


def test_无命中时返回空结果而非失败(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "绝不存在的标识符xyzzy"})
    assert result.ok is True
    assert result.content.strip() == ""


def test_搜索不逃出项目根(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_probe.py"
    outside.write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "SECRET_MARKER"})
    assert "SECRET_MARKER" not in result.content


# --------------------------------------------------------------------------
# 读不了的路径：跳过 + 如实报告
# --------------------------------------------------------------------------


def test_部分路径读不了要跳过而不是整条失败(tmp_path: Path, monkeypatch) -> None:
    """能读的地方已经搜过了，结果是可用的——不许因为一处读不了就全丢掉。"""
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    locked = tmp_path / "pytest-cache-files-locked"
    locked.mkdir()
    _fake_run(
        monkeypatch,
        _completed(
            stdout=f"{tmp_path / 'a.py'}:1:def target():\n",
            stderr=f"rg: {locked}: 拒绝访问。 (os error 5)\n",
        ),
    )

    result = search_code_spec(tmp_path).handler({"pattern": "def target"})

    assert result.ok is True, result.content
    assert "a.py" in result.content, "能读的地方的命中不许丢"
    # 跳过的必须可见（不许静默）：告诉它「这里没搜到」，而不是让它以为「这里没有」。
    assert "跳过 1 个不可读路径" in result.content
    assert "pytest-cache-files-locked" in result.content
    assert "不是「它们里面没有匹配」" in result.content


def test_没有命中但有读不了的路径也要说清楚(tmp_path: Path, monkeypatch) -> None:
    """空结果本身是合法的（rg 退 1），但「有地方没搜」这件事不能吞掉。"""
    locked = tmp_path / "pytest-cache-files-locked"
    locked.mkdir()
    _fake_run(
        monkeypatch,
        _completed(stdout="", stderr=f"rg: {locked}: 拒绝访问。 (os error 5)\n"),
    )

    result = search_code_spec(tmp_path).handler({"pattern": "绝不存在的标识符xyzzy"})

    assert result.ok is True
    assert result.content.strip() != "", "不是空结果：跳过了什么必须看得见"
    assert "跳过 1 个不可读路径" in result.content


def test_起点本身就读不了要给人话(tmp_path: Path, monkeypatch) -> None:
    """一次都没搜成，不许拿空结果冒充成功。"""
    locked = tmp_path / "locked"
    locked.mkdir()
    _fake_run(
        monkeypatch,
        _completed(stdout="", stderr=f"rg: {locked}: 拒绝访问。 (os error 5)\n"),
    )

    result = search_code_spec(tmp_path).handler({"pattern": "x", "path": "locked"})

    assert result.ok is False, "一条都没搜就不算成功"
    assert "没有搜成" in result.content
    assert "一条都没搜" in result.content
    assert "不是「没有匹配」" in result.content
    assert "跳过 1 个不可读路径" in result.content


def test_别的错误仍然算失败(tmp_path: Path, monkeypatch) -> None:
    """跳过只对「读不了」这一条生效：正则写错必须照样报失败，不许静默。"""
    _fake_run(
        monkeypatch,
        _completed(
            stdout="",
            stderr="regex parse error:\n    (\n    ^\nerror: unclosed group\n",
        ),
    )

    result = search_code_spec(tmp_path).handler({"pattern": "("})

    assert result.ok is False
    assert "搜索失败" in result.content


def test_真锁死的目录在真rg下报错被如实解析() -> None:
    """不 monkeypatch 的那一条：本机真实存在的锁死目录，走一遍真 rg。

    这里**显式把搜索起点指向锁死目录**，而不是指望"搜索整个仓库时会碰到它"。
    原因是一次真实的假红：本机仓库根下只有一个被 ACL 锁死的目录 `.pytest_cache`，
    而 rg 默认不进点开头的目录——于是它根本不会去读那个目录，也就不会报错，
    用例却因为"机器上确实有锁死目录"而开跑，最后红在断言上。显式路径不受忽略
    规则影响，rg 一定会去读它，于是「解析 rg 报错 → 跳过 + 如实报告」这条真机
    路径稳定可走。

    这台机器上一个锁死目录都没有时跳过——判据本身由上面四条 monkeypatch 用例钉住。
    """
    locked = _locked_dirs()
    if not locked:
        pytest.skip("本机没有读不了的目录；monkeypatch 那几条兜底")

    target = locked[0]
    result = search_code_spec(REPO_ROOT).handler({"pattern": "def ", "path": target.name})

    assert result.ok is False, "一条都没搜就不算成功"
    assert "没有搜成" in result.content
    # 跳过的必须可见且指名道姓（只报个数字没法让人去修）。
    assert "跳过 1 个不可读路径" in result.content
    assert target.name in result.content
