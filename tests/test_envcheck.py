"""测量环境自证（S1 / 问题 O1：测量环境不干净）。

这组用例钉的是四件事，缺一条都可能重新长出"环境问题被洗成能力问题"：

1. **缺 rg 就不合格、退出码 3**：A 类格子假 0/3 的根因就是子进程里没有 rg；
2. **代理没绕开本机回环就不合格**：真机证据是 `/tokenize` 被代理回 502、整批
   读数静默变成"未完成"；
3. **httpx 构造点必须都带 `trust_env=False`**：新加客户端忘了这一条时当场红；
4. **候选目录与"补 PATH"只有一份实现**：真机跑分脚本以前自己另建了一套环境，
   绕过补 PATH——这条钉的就是"不许再长第二份"。
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from spoolkit import envcheck

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
SCRIPTS = REPO_ROOT / "scripts"
COUNTERFACTUAL = (
    REPO_ROOT / "benchmarks" / "probes" / "orchestration" / "real_counterfactual.py"
)

if str(SCRIPTS) not in sys.path:  # 与 tests/test_probe_runner.py 同一条路
    sys.path.insert(0, str(SCRIPTS))

import probe as probe_runner  # noqa: E402


def _whose_which_found(monkeypatch) -> None:
    """假装 rg 在 PATH 里（只为了把"工具齐不齐"这一维从判据里摘出去）。"""
    monkeypatch.setattr(
        envcheck, "which", lambda name, path=None: rf"C:\fake\{name}.exe"
    )


# --- 1. 必需工具 ------------------------------------------------------------


def test_缺必需工具就不合格且退出码是3(monkeypatch) -> None:
    monkeypatch.setattr(envcheck, "which", lambda name, path=None: None)
    report = envcheck.inspect(env={"PATH": ""}, required=("rg",), window=4096)

    assert report.ok is False
    assert report.missing == ("rg",)
    assert report.effective is not None and report.effective < 4096
    with pytest.raises(envcheck.EnvironmentUnusable) as caught:
        envcheck.require(report)
    assert "缺必需工具" in str(caught.value)
    assert envcheck.require_or_exit(report, stream=io.StringIO()) == (
        envcheck.EXIT_ENVIRONMENT_BLOCKED
    )


def test_可选工具缺失只记进报告不判死(monkeypatch) -> None:
    monkeypatch.setattr(
        envcheck,
        "which",
        lambda name, path=None: rf"C:\fake\{name}.exe" if name == "rg" else None,
    )
    report = envcheck.inspect(env={"PATH": ""}, required=("rg",))

    assert report.ok is True
    assert report.optional_missing == ("node",)
    assert envcheck.require_or_exit(report, stream=io.StringIO()) == 0


def test_补上PATH之后合格(tmp_path, monkeypatch) -> None:
    for name in ("rg", "rg.exe", "node", "node.exe"):
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(envcheck, "probe_tool_dirs", lambda: [tmp_path])

    env = {"PATH": ""}
    added = envcheck.ensure_probe_tools(env, ("rg", "node"))

    assert added and any(one.startswith("rg@") for one in added)
    assert env["PATH"].startswith(str(tmp_path))
    report = envcheck.inspect(env=env, required=("rg",))
    assert report.ok is True
    assert report.path_added == tuple(added)


def test_候选目录清单可以被开关关掉(monkeypatch) -> None:
    monkeypatch.setenv(envcheck.NO_TOOL_DIRS_ENV, "1")
    assert envcheck.probe_tool_dirs() == []


# --- 2. 代理 ----------------------------------------------------------------


def test_代理没绕开本机回环就不合格(monkeypatch) -> None:
    _whose_which_found(monkeypatch)
    env = {"PATH": "", "HTTP_PROXY": "http://127.0.0.1:7890"}

    report = envcheck.inspect(env=env, required=("rg",))

    assert report.ok is False
    assert "127.0.0.1" in report.proxy_problem
    assert "502" in report.proxy_problem
    assert "回环" in envcheck.fix_hint(report)


def test_代理绕开了本机回环就合格(monkeypatch) -> None:
    _whose_which_found(monkeypatch)
    env = {
        "PATH": "",
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }

    report = envcheck.inspect(env=env, required=("rg",))

    assert report.proxy_problem == ""
    assert report.ok is True


def test_NO_PROXY漏掉一个回环主机也不合格(monkeypatch) -> None:
    _whose_which_found(monkeypatch)
    env = {"PATH": "", "HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "127.0.0.1"}

    report = envcheck.inspect(env=env, required=("rg",))

    assert report.proxy_problem != ""
    assert "localhost" in report.proxy_problem


def test_方括号写法进说明但不判死(monkeypatch) -> None:
    _whose_which_found(monkeypatch)
    env = {
        "PATH": "",
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "127.0.0.1,localhost,[::1]",
    }

    report = envcheck.inspect(env=env, required=("rg",))

    assert report.ok is True  # [::1] 语义上确实覆盖了 ::1
    assert any("方括号" in note for note in report.notes)


def test_摘掉代理变量并给回环写NO_PROXY() -> None:
    env = {"HTTP_PROXY": "http://127.0.0.1:7890", "PATH": ""}

    removed = envcheck.isolate_local_http(env)

    assert removed == {"HTTP_PROXY": "http://127.0.0.1:7890"}
    assert "HTTP_PROXY" not in env
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert envcheck.proxy_problem(env) == ""


# --- 3. httpx 代理防御 ------------------------------------------------------


def test_审计抓到没关环境代理的构造点(tmp_path) -> None:
    (tmp_path / "bad.py").write_text(
        "import httpx\n\nclient = httpx.Client(\n    timeout=1,\n)\n", encoding="utf-8"
    )
    (tmp_path / "good.py").write_text(
        "import httpx\n\nclient = httpx.AsyncClient(trust_env=False)\n", encoding="utf-8"
    )
    (tmp_path / "true.py").write_text(
        "import httpx\n\nclient = httpx.Client(trust_env=True)\n", encoding="utf-8"
    )

    gaps = envcheck.audit_httpx_trust_env(tmp_path)

    assert gaps == ["bad.py:3", "true.py:3"]


def test_仓库自己的构造点一个都不漏() -> None:
    gaps = envcheck.audit_httpx_trust_env(SRC / "spoolkit")

    assert gaps == [], f"这些 httpx 构造点没关环境代理（补 trust_env=False）：{gaps}"


def test_构造点漏掉就判不合格(monkeypatch, tmp_path) -> None:
    _whose_which_found(monkeypatch)
    (tmp_path / "bad.py").write_text(
        "import httpx\n\nclient = httpx.Client(timeout=1)\n", encoding="utf-8"
    )

    report = envcheck.inspect(env={"PATH": ""}, required=("rg",), source_root=tmp_path)

    assert report.ok is False
    assert report.trust_env_gaps == ("bad.py:3",)
    with pytest.raises(envcheck.EnvironmentUnusable):
        envcheck.require(report)


# --- 4. 只有一份实现（真机跑分那条路必须走它）--------------------------------


def test_候选目录没有第二份实现() -> None:
    for path in (SCRIPTS / "probe.py", COUNTERFACTUAL):
        text = path.read_text(encoding="utf-8")
        assert "SummerEngine" not in text, f"{path} 里又长出了一份候选目录清单"
        assert "cursor-agent" not in text, f"{path} 里又长出了一份候选目录清单"
    assert "SummerEngine" in (SRC / "spoolkit" / "envcheck.py").read_text(encoding="utf-8")


def test_探针的候选目录就是envcheck那一份() -> None:
    assert probe_runner._envcheck() is envcheck
    assert probe_runner._tool_dirs() == envcheck.probe_tool_dirs()


def test_真机跑分脚本接到了环境自证() -> None:
    text = COUNTERFACTUAL.read_text(encoding="utf-8")

    # 当前进程补 PATH：A 类假 0/3 的根因就是只在子进程里补、进程内没补
    assert "envcheck.ensure_probe_tools(os.environ" in text
    # 开跑前自证**被调用**（不是只留一个定义）
    assert re.search(r"^\s+_prepare_process_env\(\)$", text, re.M), (
        "真机跑分脚本没有在开跑前调 `_prepare_process_env()`"
    )
    # 每条读数自带环境（合并多批读数时也能解释）
    assert 'record["env"]' in text


def test_探针子进程环境里补了PATH(monkeypatch, tmp_path) -> None:
    for name in ("rg", "rg.exe"):
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(envcheck, "which", lambda name, path=None: None)
    monkeypatch.setattr(envcheck, "probe_tool_dirs", lambda: [tmp_path])
    before = os.environ.get("PATH", "")

    env = probe_runner.probe_env(tmp_path / "sandbox")

    assert env["PATH"].startswith(str(tmp_path))
    assert env["_PROBE_PATH_ADDED"].startswith("rg@")
    # 探针自己的 PATH 不许被改：补的只是子进程那一份。
    assert os.environ.get("PATH", "") == before


def test_check_env在抽掉rg时退出码3(tmp_path) -> None:
    """端到端：**故意抽掉 rg**（PATH 清空 + 关掉候选目录），探针必须失败退出。"""
    env = {
        "PATH": "",
        "PYTHONIOENCODING": "utf-8",
        envcheck.NO_TOOL_DIRS_ENV: "1",
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }

    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "probe.py"), "--check-env"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    assert done.returncode == envcheck.EXIT_ENVIRONMENT_BLOCKED, (
        f"抽掉 rg 之后探针居然没失败：exit={done.returncode}\n"
        f"{done.stdout}\n{done.stderr}"
    )
    assert "判定：不合格" in done.stdout
    assert "缺必需工具" in done.stdout


def test_探针自证在工具齐时通过(monkeypatch) -> None:
    _whose_which_found(monkeypatch)

    code, report = probe_runner.env_self_check(window=8192)

    assert code == 0
    assert report.ok is True
