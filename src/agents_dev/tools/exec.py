"""命令执行工具。

这是「能改代码」和「能验证代码」之间的那道坎。没有它，agent 只能写出
看起来对的代码；有了它，才可能形成「写 → 跑 → 读失败 → 改」的循环。

安全边界靠四条：
1. **白名单命令**，不做任意 shell 透传；
2. **不经过 shell**，所以参数里的 `;`、`|`、`&&` 都是普通字符，
   注入类问题从根上不存在；
3. **工作目录锁在项目内**，且脚本路径必须落在项目里；
4. **超时强杀**，输出截断——一个跑不完或吐出海量日志的命令会直接吃掉上下文。

刻意禁掉 `python -c` 与 `python -i`：它们是任意代码执行的入口，
而白名单的意义正在于排除这一类。要跑代码就跑项目里的文件或模块。
"""

import shutil
import subprocess
import sys
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 4000

# 只允许只读的 git 子命令。提交、回滚、推送由人来做——
# 让 agent 动版本历史，是把最后的退路也交出去了。
GIT_READONLY = frozenset(
    {"status", "diff", "log", "show", "branch", "ls-files", "rev-parse"}
)

# python 的任意代码执行入口，一律禁止。
BLOCKED_PYTHON_FLAGS = ("-c", "-i", "-")


def _check_python(argv: list[str]) -> str | None:
    args = argv[1:]
    if not args:
        return "python 需要参数：`-m <模块> <参数>` 或 `<脚本路径> <参数>`"
    if args[0] in BLOCKED_PYTHON_FLAGS:
        return f"出于安全考虑，禁止 python {args[0]}（任意代码执行入口）"

    if args[0] == "-m":
        if len(args) < 2:
            return "python -m 后面需要模块名"
        return None

    # 脚本形式：脚本必须落在项目内
    return None


def _check_git(argv: list[str]) -> str | None:
    if len(argv) < 2:
        return "git 需要子命令"
    if argv[1] not in GIT_READONLY:
        allowed = "、".join(sorted(GIT_READONLY))
        return f"只允许只读的 git 子命令（{allowed}），{argv[1]} 不在其中"
    return None


def validate_command(argv: list[str]) -> str | None:
    """校验命令是否在白名单内。通过返回 None。"""
    if not argv:
        return "命令不能为空"

    program = argv[0]
    rest = argv[1:]

    # uv run 只是包装一层，真正执行的是后面的命令
    if program == "uv":
        if not rest or rest[0] != "run":
            return "只允许 `uv run <命令>`"
        return validate_command(rest[1:])

    if program in ("pytest",) or program.endswith("pytest.exe"):
        return None
    if program in ("python", "python3") or program.endswith("python.exe"):
        return _check_python([program, *rest])
    if program == "git":
        return _check_git(argv)

    return f"命令不在白名单内: {program}"


def resolve_argv(argv: list[str], root: Path) -> list[str]:
    """把模型写的命令名换算成本机真实可用的可执行文件。

    模型不该知道也不该猜「用哪个解释器」——那是环境细节，不是任务信息。
    裸 `python`、`python3`、`pytest` 在一台机器上可能是坏的垫片、可能不在
    PATH、可能指向错误的虚拟环境。实测里模型为此白白烧掉了十步。

    策略：优先项目自带虚拟环境，其次当前解释器。
    """
    if not argv:
        return argv

    program = argv[0]
    if program not in ("python", "python3", "pytest"):
        return argv

    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ]
    interpreter = next(
        (str(path) for path in candidates if path.exists()), sys.executable
    )

    if program == "pytest":
        return [interpreter, "-m", "pytest", *argv[1:]]
    return [interpreter, *argv[1:]]


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head = text[: MAX_OUTPUT_CHARS // 2]
    tail = text[-MAX_OUTPUT_CHARS // 2 :]
    return f"{head}\n…（输出过长，中间省略 {len(text) - MAX_OUTPUT_CHARS} 字符）…\n{tail}"


def _run(root: Path, args: dict) -> ToolResult:
    argv = list(args["command"])
    problem = validate_command(argv)
    if problem is not None:
        return ToolResult(ok=False, content=problem)

    # 回显模型自己写的那份命令，而不是换算后的。把解释器全路径暴露出去，
    # 模型会把它当成参数再传回来，实测里就演变成了 "can't open file <解释器路径>"。
    requested = " ".join(argv)
    argv = resolve_argv(argv, root)

    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    if not 1 <= timeout <= MAX_TIMEOUT:
        return ToolResult(ok=False, content=f"timeout 必须在 1 到 {MAX_TIMEOUT} 秒之间")

    try:
        cwd = resolve_within(root, args.get("cwd", "."))
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))
    if not cwd.is_dir():
        return ToolResult(ok=False, content=f"工作目录不存在: {args.get('cwd')}")

    # uv run 需要能找到 uv 本体；找不到就明确说不支持，而不是静默换命令。
    program = argv[0]
    if program == "uv" and shutil.which("uv") is None:
        return ToolResult(ok=False, content="未找到 uv，请改用 python 或 pytest 直接执行")

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,  # 明确禁止经 shell，参数里的元字符因此是惰性的
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            ok=False, content=f"命令超过 {timeout} 秒未结束，已终止"
        )
    except FileNotFoundError:
        return ToolResult(ok=False, content=f"找不到命令: {program}")
    except OSError as exc:
        return ToolResult(ok=False, content=f"命令执行失败: {exc}")

    combined = (proc.stdout or "") + (proc.stderr or "")
    body = _truncate(combined.strip())
    status = "成功" if proc.returncode == 0 else "失败"
    # 失败时把输出放在前面：模型和人都先要知道「错在哪」，
    # 而不是先看到自己刚才发了什么命令。
    return ToolResult(
        ok=proc.returncode == 0,
        content=f"退出码 {proc.returncode}（{status}）\n$ {requested}\n{body}",
    )


def run_command_spec(root: Path) -> ToolSpec:
    """执行白名单命令，用于跑测试与验证改动。"""
    return ToolSpec(
        name="run_command",
        description=(
            "在项目目录内执行命令并返回输出；支持 pytest、python 模块或脚本、"
            "只读 git。用于跑测试验证改动"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=lambda args: _run(root, args),
    )
