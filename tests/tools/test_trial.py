import os
from pathlib import Path

from spoolkit.tools.edit import PendingChanges, replace_lines_spec
from spoolkit.tools.exec import run_command_spec
from spoolkit.tools.trial import copy_project, trial_workspace


def _project(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    for heavy in (".venv", ".git", "__pycache__"):
        target = tmp_path / heavy
        target.mkdir()
        (target / "junk.txt").write_text("x", encoding="utf-8")


def test_副本排除虚拟环境与缓存(tmp_path: Path) -> None:
    _project(tmp_path)
    target = tmp_path / "_copy"
    copy_project(tmp_path, target)
    assert (target / "calc.py").exists()
    assert not (target / ".venv").exists()
    assert not (target / ".git").exists()
    assert not (target / "__pycache__").exists()


def test_副本里已应用未落盘的改动(tmp_path: Path) -> None:
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2,
         "content": "    return a + b\n", "expect": "    return a - b"}
    )
    with trial_workspace(tmp_path, pending.items()) as work:
        assert "a + b" in (work / "calc.py").read_text(encoding="utf-8")
    # 真实文件不受影响
    assert "a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_试跑目录退出后被清理(tmp_path: Path) -> None:
    _project(tmp_path)
    with trial_workspace(tmp_path, []) as work:
        base = work.parent
    assert not base.exists()


def test_有未落盘改动时测试跑在副本上(tmp_path: Path) -> None:
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2,
         "content": "    return a + b\n", "expect": "    return a - b"}
    )
    result = run_command_spec(tmp_path, pending).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    # 改动尚未落盘，但试跑环境里已经生效，所以测试应当通过
    assert result.ok is True, result.content
    assert "1 passed" in result.content


def test_真实文件在试跑后仍是旧内容(tmp_path: Path) -> None:
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2,
         "content": "    return a + b\n", "expect": "    return a - b"}
    )
    run_command_spec(tmp_path, pending).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    assert "a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_没有待落盘改动时在原地执行(tmp_path: Path) -> None:
    _project(tmp_path)
    result = run_command_spec(tmp_path, PendingChanges(tmp_path)).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    # 原文件有 bug，测试应当失败——说明确实跑在原地
    assert result.ok is False
    assert "1 failed" in result.content


def test_试跑副本不靠测试侧补丁也能建出来(
    tmp_path: Path, raw_dir_creation
) -> None:
    """试跑副本的创建点得自己抬 mode，不能指望测试插件的补丁。

    受限环境里 `mkdtemp` 写死 0o700，建出来的目录当场不可访问，紧接着的
    `copytree` 连 makedirs 都过不去——自动验证于是报「环境问题」，
    而「改完能不能自己验」正是回归集要量的东西。改回裸 `mkdtemp`，这里就是红的。
    """
    _project(tmp_path)
    with trial_workspace(tmp_path, []) as work:
        assert os.listdir(work)  # 旧写法：走到这行就 PermissionError
        assert (work / "calc.py").exists()
        base = work.parent
    assert not base.exists()  # 摘掉补丁也不影响退出时清理


# --- 依赖目录：接进副本，而不是"整个拿掉" ------------------------------------


def _with_deps(tmp_path: Path) -> None:
    _project(tmp_path)
    pkg = tmp_path / "node_modules" / "some-pkg"
    pkg.mkdir(parents=True)
    (pkg / "index.js").write_text("export const x = 1\n", encoding="utf-8")


def test_依赖目录接进副本(tmp_path: Path) -> None:
    """Node 项目的依赖就在 `node_modules` 里，且不是全局的——副本里没有它，
    `npm run build` 必然报"找不到模块"，模型会把这当成"依赖没装"去重装。
    实测（Vue 项目）白烧了 6 步预算。链接不占空间、副本里的相对路径照旧成立。
    """
    _with_deps(tmp_path)
    report: dict = {}
    with trial_workspace(tmp_path, [], report=report) as work:
        assert (work / "node_modules" / "some-pkg" / "index.js").is_file()
        assert "node_modules" in report["linked"]
        assert report["skipped"] == []
        assert (work / "calc.py").exists()  # 项目内容照旧
    # 最要紧的一条：清理副本不许顺着链接把真实依赖删掉
    assert (tmp_path / "node_modules" / "some-pkg" / "index.js").is_file()
    assert (tmp_path / "calc.py").exists()


def test_接不上的依赖目录会被如实报出来(tmp_path: Path, monkeypatch) -> None:
    """接不上（没有建链接的权限）就退回"副本里没有它"，但必须记下来——
    调用方要靠它把"这是副本造成的"说给模型听。"""
    import spoolkit.tools.trial as trial_mod

    _with_deps(tmp_path)
    monkeypatch.setattr(trial_mod, "_link_dir", lambda source, target: "拒绝访问")
    report: dict = {}
    with trial_workspace(tmp_path, [], report=report) as work:
        assert not (work / "node_modules").exists()
        assert "node_modules" in report["skipped"]


def test_试跑输出开头说明自己在副本里(tmp_path: Path) -> None:
    _with_deps(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2,
         "content": "    return a + b\n", "expect": "    return a - b"}
    )
    result = run_command_spec(tmp_path, pending).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    assert "试跑副本" in result.content
    assert "node_modules" in result.content
    assert "1 passed" in result.content  # 结论照旧在
    # 没有待落盘改动时原地执行，不该出现这段说明
    plain = run_command_spec(tmp_path, PendingChanges(tmp_path)).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    assert "试跑副本" not in plain.content

