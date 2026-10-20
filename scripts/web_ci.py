"""网页壳的构建与检查。开发机上跑；用户运行 spool web 不需要 Node。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], env: dict[str, str] | None = None) -> int:
    print("+", " ".join(cmd), flush=True)
    done = subprocess.run(cmd, cwd=ROOT, env=env)
    return done.returncode


def main() -> int:
    python = sys.executable
    codes = [
        _run([python, str(ROOT / "scripts" / "build_web_assets.py")]),
        _run([python, str(ROOT / "scripts" / "check_web_assets.py")]),
    ]
    node = shutil.which("node")
    if node is None:
        print("node 不在 PATH")
        return 1
    codes.append(_run([
        node, "--test", str(ROOT / "tests" / "web" / "js" / "pure.test.mjs"),
    ]))
    if not (ROOT / "web" / "node_modules" / "esbuild").exists():
        print("还没有 web/node_modules，先在 web/ 里 npm install")
        return 1
    codes.append(_run([node, str(ROOT / "web" / "build.mjs")]))
    tsc = ROOT / "web" / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.is_file():
        print("还没有 typescript，先在 web/ 里 npm install")
        return 1
    codes.append(_run([node, str(tsc), "-p", str(ROOT / "web" / "tsconfig.json")]))
    codes.append(_run([
        "git", "diff", "--exit-code", "--",
        "src/spoolkit/web/assets/dist",
        "src/spoolkit/web/assets/index.html",
        "src/spoolkit/web/assets/manifest.json",
    ]))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / "scripts")])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTEST_DEBUG_TEMPROOT", str(ROOT / ".tmp" / "ptroot-cursor"))
    env.setdefault("PYTEST_ADDOPTS", "-p pytest_sandbox_compat")
    Path(env["PYTEST_DEBUG_TEMPROOT"]).mkdir(parents=True, exist_ok=True)
    codes.append(_run([
        python, "-m", "pytest", "-q",
        "-p", "no:cacheprovider",
        "-p", "pytest_sandbox_compat",
        "tests/web",
        "tests/kernel/test_sessions.py",
        "tests/mcp/test_server.py",
        "tests/agent/test_task_contract.py",
    ], env=env))
    return 0 if all(code == 0 for code in codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
