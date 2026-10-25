"""开发环境的**单一事实来源**：能力体检 + 环境准备。

为什么要有这一层：受限沙箱下「哪些能力可用」直接决定测试/工具怎么跑，而这段
知识原先散在三个地方各写一遍（`scripts/pytest.ps1`、`scripts/gate.py`、探针的
`env_self_check`）——一处修不处处生效，于是同一类坑每次都从症状开始重查。

用法：

    python scripts/dev_env.py              # 打印能力体检表
    python scripts/dev_env.py --print-env  # 打印跑测试要设的环境变量（KEY=VALUE 一行）
    python scripts/dev_env.py --json       # 机读（供 gate / 测试用）

**能力名是稳定的**：测试用 `@pytest.mark.needs("process_kill")` 声明依赖，
缺能力时由 `scripts/pytest_needs.py` 跳过并说明原因——比"红了再猜"根本得多。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE_ROOT = REPO / ".tmp" / "cache"

# 仓库源码要能 import：`sandbox_dirs.install()` 是"临时目录救回来"的**唯一实现**，
# 而本模块要在 pytest 之外也用它（探针、gate 的体检、任何自己建临时目录的工具）。
_SRC = REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
try:
    from spoolkit.sandbox_dirs import install as _install_dir_compat
except Exception:  # noqa: BLE001 - 源码不可用时退化成"照实探测"
    _install_dir_compat = None


def install_dir_compat() -> bool:
    """装一次"临时目录 0o777"补丁。**必须在建任何临时目录之前调用**。

    为什么不能事后补救：用 `mode=0o700` 建出来的目录，之后连
    `os.chmod(p, 0o777)` 也是 `WinError 5`（`src/spoolkit/sandbox_dirs.py:11`），
    进去/往下建子目录一律被拒。所以只能"建的时候就别用 0o700"。
    """
    if _install_dir_compat is None:
        return False
    _install_dir_compat()
    return True

@dataclass(frozen=True)
class Capability:
    """一条宿主能力：名字 + 人话标签 + **所以呢**。

    `workaround` 是这条能力不可用时**受限环境下也能走的替代**——必填。
    它存在的理由：体检只说"能不能"，而卡住的人需要的是"那我现在怎么办"。
    这一跳以前只活在聊天记录里，于是同一个弯路每个会话都要重走一次
    （本会话实测：`git checkout` 连撞两次才改用 `edit`；`.venv` 写不进去
    才想到 `--target`）。

    `escalate` 是"这件事必须做成、没有替代"时对用户的那**一句请求**
    （需要切一次完全权限做什么）；`needs_unconfined` 标出"没有受限替代"。
    """

    label: str
    workaround: str
    escalate: str = ""
    needs_unconfined: bool = False


#: 能力登记表。**每一条都必须写出 `workaround`**（`tests/test_dev_env.py` 钉着），
#: 否则它会退化成"只报错、不给路"的装置——那比不报还费人。
CAPABILITIES: dict[str, Capability] = {
    "workspace_write": Capability("仓内建/删文件", "正常；受限会话里仓内是可写的"),
    "tmpdir_write": Capability(
        "临时目录可写（含 0o700 修正）",
        "dev_env 已自动装目录补丁（`sandbox_dirs.install()`，把建目录的 mode 抬成 0o777）；"
        "自己建临时目录前先调 `dev_env.install_dir_compat()`",
    ),
    "venv_write": Capability(
        "写 .venv（装依赖用）",
        "装工具用 `uv pip install --target .tmp/tools-<名字>`（本会话装 mypy 就是这么做的），"
        "或 `uvx --from <包>` 起临时环境",
        escalate="需要装/升级项目依赖时，切一次「完全权限」跑 `uv sync --extra dev`",
        needs_unconfined=True,
    ),
    "tool_file_write": Capability(
        "子进程改「工具层创建的文件」（git 还原依赖它）",
        "改文件用 `edit` 工具（它走受控通道）；**别再重试** `git checkout/stash`、"
        "`Copy-Item`、Python `write_bytes` 去覆盖它们——本会话连撞两次，都是白花时间",
        escalate="非要用 git 做整批还原时，切一次「完全权限」再跑",
        needs_unconfined=True,
    ),
    "python_pipe": Capability("python 子进程管道", "正常；管住的是 node 那一侧"),
    "node_pipe": Capability(
        "node 子进程管道（esbuild / vitest 依赖它）",
        "受限会话里不跑 `vite build` / `vitest run`；类型检查走 `vue-tsc --noEmit`"
        "（探针判据里已经这么分档：结构 + 类型照判，构建如实标「环境未验」）",
        escalate="需要真实构建/单测证据时，切一次「完全权限」或在非受限终端里跑",
        needs_unconfined=True,
    ),
    "process_kill": Capability(
        "按进程树杀进程",
        "要它的用例打 `@pytest.mark.needs(\"process_kill\")` —— 缺能力时跳过并说明"
        "（`tests/cli/test_service_command.py` 就是这么做的），**红不是回归**；"
        "别把 `assert 1 == 0` 当成代码坏了",
        escalate="要在真机上验证服务启停（`spool service`）时，切一次「完全权限」跑那几条用例",
        needs_unconfined=True,
    ),
    "rg": Capability(
        "PATH 里有 rg",
        "dev_env 会自动把常见位置（cursor 自带的 ripgrep）补进 PATH，一般不用管",
        escalate="补不上就装一个 ripgrep（`winget install BurntSushi.ripgrep.MSVC`）",
    ),
    "node": Capability(
        "PATH 里有 node",
        "只用 Python 侧的关口时不需要它；探针的前端那几条才要",
        escalate="装 Node（本机的 nvm4w 就有）",
    ),
}


def render(capabilities: dict[str, dict[str, object]] | None = None) -> str:
    caps = capabilities or probe_capabilities()
    width = max(len(CAPABILITIES[name].label) for name in caps) + 2
    lines = [f"仓库：{REPO}", f"python：{sys.executable} ({sys.version.split()[0]})"]
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or ""
    lines.append(f"代理：{proxy or '（无）'}   临时目录：{tempfile.gettempdir()}")
    lines.append("")
    for name, item in caps.items():
        info = CAPABILITIES[name]
        verdict = "可用" if item["ok"] else "不可用"
        lines.append(f"{info.label:<{width}}{verdict:<8}{item['detail']}")
        # **所以呢**：只有不可用的时候才印，避免把表撑成一片噪声。
        if not item["ok"]:
            lines.append(f"{'':<{width}}  → 受限下这么做：{info.workaround}")
            if info.escalate:
                lines.append(f"{'':<{width}}  → {info.escalate}")
    lines.append("")
    lines.append(f"判定：{'受限（按沙箱兼容策略跑）' if is_sandboxed(caps) else '非受限（豁免机制失效）'}")
    return "\n".join(lines)


def _try(func) -> tuple[bool, str]:
    try:
        ok, detail = func()
        return bool(ok), str(detail)
    except Exception as exc:  # noqa: BLE001 - 体检就是要看异常
        return False, f"{type(exc).__name__}: {exc}"


def probe_capabilities() -> dict[str, dict[str, object]]:
    """逐条探测。**只看能力，不改仓库状态**（临时物都清掉）。

    探测前先装目录补丁：要回答的是"用上我们的兼容层之后，这项能力能不能用"，
    而不是"裸 Python 能不能用"。
    """
    install_dir_compat()
    out: dict[str, dict[str, object]] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        out[name] = {"ok": ok, "detail": detail}

    def workspace_write():
        path = CACHE_ROOT.parent / f".dev-env-probe-{uuid.uuid4().hex[:6]}.tmp"
        path.write_text("x", encoding="utf-8")
        path.unlink()
        return True, "仓内可写可删"

    def tmpdir_write():
        base = Path(tempfile.mkdtemp(prefix="dev-env-"))
        # `mkdtemp` 默认 0o700，而 0o700 的目录在沙箱里**建完就进不去**
        # （连 chmod 都救不回来）——所以这里的"可用"是**装上目录补丁之后**的
        # 结论，与 pytest 走的是同一套 `sandbox_dirs.install()`。
        (base / "a.txt").write_text("x", encoding="utf-8")
        shutil.rmtree(base)
        return True, f"{base.parent}（目录补丁已装）"

    def venv_write():
        path = REPO / ".venv" / f".dev-env-probe-{uuid.uuid4().hex[:6]}.tmp"
        path.write_text("x", encoding="utf-8")
        path.unlink()
        return True, "可写可删"

    def tool_file_write():
        # 这条**抽样**看：沙箱里"工具层创建的文件子进程能不能改"是**逐个文件**
        # 不同的（本会话实测：`pyproject.toml` 可写、`src/spoolkit/cli/events.py`
        # 不可写）。它决定 `git checkout/restore`、`git stash` 这些**还原手段**
        # 能不能用，所以按"几个代表文件里可写几成"报，并把不可写的列出来。
        samples = [
            REPO / "pyproject.toml",
            REPO / "README.md",
            REPO / "src" / "spoolkit" / "cli" / "events.py",
            REPO / "src" / "spoolkit" / "settings.py",
            REPO / "tests" / "test_secrets.py",
        ]
        blocked: list[str] = []
        for path in samples:
            if not path.is_file():
                continue
            try:
                # `r+`：不截断、不写内容，纯粹问一句"能不能改"。
                with path.open("r+", encoding="utf-8"):
                    pass
            except OSError:
                blocked.append(path.relative_to(REPO).as_posix())
        writable = len([p for p in samples if p.is_file()]) - len(blocked)
        if not blocked:
            return True, f"{writable} 个样本全可写（git 还原可用）"
        return False, f"不可写：{', '.join(blocked)}（这些文件上 git 还原会被拒）"

    def python_pipe():
        done = subprocess.run(
            [sys.executable, "-c", "print(1)"], capture_output=True, text=True, timeout=60
        )
        return done.stdout.strip() == "1", "piped stdio 正常"

    def node_pipe():
        code = (
            "const{spawnSync}=require('child_process');"
            "const r=spawnSync(process.execPath,['-e','console.log(1)'],{encoding:'utf8'});"
            "console.log(r.error?('ERR '+r.error.code):r.stdout.trim());"
        )
        done = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=90)
        text = (done.stdout or "").strip()
        return text == "1", text or (done.stderr or "").strip()[:80]

    def process_kill():
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if os.name == "nt":
                done = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(child.pid)],
                    capture_output=True, text=True, timeout=60,
                )
                return done.returncode == 0, (done.stdout or done.stderr or "").strip()[:80]
            os.killpg(os.getpgid(child.pid), 9)
            return True, "killpg 成功"
        finally:
            try:
                child.kill()
            except OSError:
                pass
            child.wait(timeout=10)

    add("workspace_write", *_try(workspace_write))
    add("tmpdir_write", *_try(tmpdir_write))
    add("venv_write", *_try(venv_write))
    add("tool_file_write", *_try(tool_file_write))
    add("python_pipe", *_try(python_pipe))
    add("node_pipe", *_try(node_pipe) if shutil.which("node") else (False, "PATH 里没有 node"))
    add("process_kill", *_try(process_kill))
    # rg 报的是"**环境准备之后**能不能用"：PATH 里没有就去常见位置补，
    # 与 `test_env()` 补 PATH 是同一套（否则体检说不可用、跑起来却能用）。
    rg_found = shutil.which("rg") or rg_dir()
    add("rg", bool(rg_found), rg_found or "没找到（PATH 与常见位置都没有）")
    node_found = shutil.which("node")
    add("node", node_found is not None, node_found or "没找到")
    return out


def rg_dir() -> str:
    """PATH 里没有 rg 时，去几个常见位置找（探针与测试都靠它）。"""
    found = shutil.which("rg")
    if found:
        return ""
    for candidate in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/cursor/resources/app/node_modules/@vscode/ripgrep/bin",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links",
        Path("C:/Program Files/ripgrep"),
    ):
        if (candidate / "rg.exe").is_file() or (candidate / "rg").is_file():
            return str(candidate)
    return ""


def is_sandboxed(capabilities: dict[str, dict[str, object]] | None = None) -> bool:
    """判定"受限"：只要有一条**宿主能力**缺失，就按受限处理。

    用哪些条判定要想清楚：`venv_write` / `tmpdir_write` 这类"我们能自己修"的
    能力缺失也算受限——它们的正确处置是**用本模块的准备逻辑**，而不是放行豁免。
    """
    caps = capabilities or probe_capabilities()
    for name in ("tmpdir_write", "venv_write", "tool_file_write", "node_pipe", "process_kill"):
        if not caps.get(name, {}).get("ok"):
            return True
    return False


def test_env(*, hanging_log: str = "") -> dict[str, str]:
    """跑测试要的环境。**与 `pytest.ps1` 的五条同源**，但只在这里写一次。

    缓存一律**就仓**（`.tmp/cache/<工具>`）：受限会话里 `%APPDATA%` / `%LOCALAPPDATA%`
    往往不可写，而"工具默认把缓存放到用户目录"是我们踩过最多的一类坑。
    """
    env = dict(os.environ)
    install_dir_compat()
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(REPO / "scripts")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTEST_ADDOPTS"] = "-p pytest_sandbox_compat -p pytest_hang_guard -p pytest_needs"
    # 临时根必须在**仓库外**、且每次新建：放进仓库里，解析类用例会"向上找到 .agent"。
    temp_root = Path(tempfile.gettempdir()) / f"spoolkit-gate-{uuid.uuid4().hex[:8]}"
    temp_root.mkdir(parents=True, exist_ok=True)
    env["PYTEST_DEBUG_TEMPROOT"] = str(temp_root)
    env["PYTEST_HANG_LOG"] = hanging_log or str(REPO / ".tmp" / "pytest-progress.log")
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    for key, sub in (
        ("npm_config_cache", "npm"),
        ("UV_CACHE_DIR", "uv"),
        ("UV_TOOL_DIR", "uv-tools"),
        ("UV_TOOL_BIN_DIR", "uv-bin"),
        ("UV_PYTHON_INSTALL_DIR", "uv-python"),
        ("PIP_CACHE_DIR", "pip"),
    ):
        target = CACHE_ROOT / sub
        target.mkdir(parents=True, exist_ok=True)
        env[key] = str(target)
    extra_rg = rg_dir()
    if extra_rg:
        env["PATH"] = f"{extra_rg}{os.pathsep}{env.get('PATH', '')}"
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开发环境体检 / 环境准备")
    parser.add_argument("--print-env", action="store_true", help="打印 KEY=VALUE 供 shell 导入")
    parser.add_argument("--json", action="store_true", help="机读输出")
    args = parser.parse_args(argv)

    if args.json:
        caps = probe_capabilities()
        payload = {
            "sandboxed": is_sandboxed(caps),
            "capabilities": {
                name: {
                    **item,
                    "label": CAPABILITIES[name].label,
                    # 机读那一份也带上动作：壳/agent 拿它就能直接决定"绕开还是提权"。
                    "workaround": CAPABILITIES[name].workaround,
                    "escalate": CAPABILITIES[name].escalate,
                    "needs_unconfined": CAPABILITIES[name].needs_unconfined,
                }
                for name, item in caps.items()
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.print_env:
        for key, value in test_env().items():
            if key in os.environ and os.environ[key] == value:
                continue
            print(f"{key}={value}")
        return 0
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
