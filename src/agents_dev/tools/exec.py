"""命令执行工具。

这是「能改代码」和「能验证代码」之间的那道坎。没有它，agent 只能写出
看起来对的代码。

安全边界靠四条：
1. **白名单命令**，不做任意 shell 透传；
2. **不经过 shell**，参数里的 `;`、`|`、`&&` 都是普通字符；
3. **工作目录锁在项目内**；
4. **超时强杀 + 输出截断**——跑不完或吐出海量日志的命令会直接吃掉上下文。

刻意禁掉 `python -c` 与 `python -i`：它们是任意代码执行的入口，
而白名单的意义正在于排除这一类。

还有一个容易被忽略的责任：**解释器由工具解析，不由模型猜**。
裸 `python`/`python3`/`pytest` 在不同机器上可能是坏的垫片、不在 PATH、
或指向错误的虚拟环境。实测中模型为此白白烧掉了十步。
"""

import shutil
import subprocess
import sys
import re
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.trial import trial_workspace
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

# 依赖安装只放行声明式形式。
#
# `pip install <任意包>` 在能力上等价于 `python -c`——安装会执行包里的构建
# 脚本，等于把特意堵掉的任意代码执行入口又打开。而且它改的是环境，你看不见。
#
# `uv add` 改的是 pyproject.toml 与 uv.lock，是一份能走 diff 审核的文件改动。
# 后果可审阅、可回滚。这就是选它而不选 pip 的全部理由。
UV_SUBCOMMANDS = frozenset({"add", "remove", "sync", "lock"})

# 会绕过包名校验的参数，一律禁止。
BLOCKED_UV_FLAGS = (
    "-e", "--editable", "--index-url", "--extra-index-url",
    "--find-links", "--trusted-host", "--path", "--url", "-r", "--requirements",
)

PACKAGE_SPEC = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"      # 包名
    r"(\[[A-Za-z0-9,._-]+\])?"           # 可选 extras
    r"([<>=!~,][^;|&<>]*)?$"             # 可选版本约束
)


def _check_uv(rest: list[str]) -> str | None:
    if not rest:
        return "uv 需要子命令"

    sub = rest[0]
    if sub == "run":
        return validate_command(rest[1:])
    if sub not in UV_SUBCOMMANDS:
        allowed = "、".join(sorted(UV_SUBCOMMANDS | {"run"}))
        return f"uv 只允许 {allowed}，{sub} 不在其中"

    for arg in rest[1:]:
        if any(arg == flag or arg.startswith(flag + "=") for flag in BLOCKED_UV_FLAGS):
            return f"禁止 uv 参数 {arg}：它会绕过包名校验"
        if arg.startswith("-"):
            continue
        if not PACKAGE_SPEC.match(arg):
            return f"包名不合法: {arg}（只接受包名与版本约束，不接受路径或 URL）"
    return None


def _check_python(args: list[str]) -> str | None:
    if not args:
        return "python 需要参数：`-m <模块> <参数>` 或 `<脚本路径> <参数>`"
    if args[0] in BLOCKED_PYTHON_FLAGS:
        return f"出于安全考虑，禁止 python {args[0]}（任意代码执行入口）"
    if args[0] == "-m" and len(args) < 2:
        return "python -m 后面需要模块名"
    return None


def validate_command(argv: list[str]) -> str | None:
    """校验命令是否在白名单内。通过返回 None。"""
    if not argv:
        return "命令不能为空"

    program, rest = argv[0], argv[1:]

    # uv run 只是包装一层，真正执行的是后面的命令
    if program == "uv":
        return _check_uv(rest)

    if program == "pytest" or program.endswith("pytest.exe"):
        return None
    if program in ("python", "python3") or program.endswith("python.exe"):
        return _check_python(rest)
    if program == "git":
        if len(rest) < 1:
            return "git 需要子命令"
        if rest[0] not in GIT_READONLY:
            allowed = "、".join(sorted(GIT_READONLY))
            return f"只允许只读的 git 子命令（{allowed}），{rest[0]} 不在其中"
        return None

    return f"命令不在白名单内: {program}"


def resolve_argv(argv: list[str], root: Path) -> list[str]:
    """把模型写的命令名换算成本机真实可用的可执行文件。

    模型不该知道也不该猜「用哪个解释器」——那是环境细节，不是任务信息。
    优先项目自带虚拟环境，其次当前解释器。
    """
    if not argv or argv[0] not in ("python", "python3", "pytest"):
        return argv

    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    interpreter = next(
        (str(path) for path in candidates if path.exists()), sys.executable
    )

    if argv[0] == "pytest":
        return [interpreter, "-m", "pytest", *argv[1:]]
    return [interpreter, *argv[1:]]


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return (
        f"{text[:half]}\n"
        f"…（输出过长，中间省略 {len(text) - MAX_OUTPUT_CHARS} 字符）…\n"
        f"{text[-half:]}"
    )


def _execute(
    base: Path, argv: list[str], requested: str, cwd_arg: str, timeout: int
) -> ToolResult:
    """在指定目录里执行。base 可能是原目录，也可能是试跑副本。"""
    try:
        cwd = resolve_within(base, cwd_arg)
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))
    if not cwd.is_dir():
        return ToolResult(ok=False, content=f"工作目录不存在: {cwd_arg}")

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
        return ToolResult(ok=False, content=f"命令超过 {timeout} 秒未结束，已终止")
    except FileNotFoundError:
        return ToolResult(ok=False, content=f"找不到命令: {argv[0]}")
    except OSError as exc:
        return ToolResult(ok=False, content=f"命令执行失败: {exc}")

    body = _truncate(((proc.stdout or "") + (proc.stderr or "")).strip())
    status = "成功" if proc.returncode == 0 else "失败"
    # 输出放在前面：模型和人都先要知道「错在哪」，
    # 而不是先看到自己刚才发了什么命令。
    return ToolResult(
        ok=proc.returncode == 0,
        content=f"退出码 {proc.returncode}（{status}）\n$ {requested}\n{body}",
    )


def _run(root: Path, args: dict, pending=None) -> ToolResult:
    argv = list(args["command"])
    problem = validate_command(argv)
    if problem is not None:
        return ToolResult(ok=False, content=problem)

    # 回显模型自己写的那份命令，而不是换算后的。把解释器全路径暴露出去，
    # 模型会把它当成参数再传回来，实测里演变成了 "can't open file <解释器路径>"。
    requested = " ".join(argv)

    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    if not 1 <= timeout <= MAX_TIMEOUT:
        return ToolResult(ok=False, content=f"timeout 必须在 1 到 {MAX_TIMEOUT} 秒之间")

    if argv[0] == "uv" and shutil.which("uv") is None:
        return ToolResult(
            ok=False,
            content="未找到 uv，无法执行该命令。可以直接告诉用户需要手动安装依赖。",
        )

    resolved = resolve_argv(argv, root)
    cwd_arg = args.get("cwd", ".")

    # 有未落盘的改动时，在试跑副本里执行：模型以为文件已经改了，
    # 那就让它在试跑环境里确实已经改了。真实工作区全程一动不动。
    if pending is not None and len(pending):
        with trial_workspace(root, pending.items()) as work_root:
            return _execute(work_root, resolved, requested, cwd_arg, timeout)
    return _execute(root, resolved, requested, cwd_arg, timeout)


def run_command_spec(root: Path, pending=None) -> ToolSpec:
    """执行白名单命令。有未落盘改动时自动在试跑副本上执行。"""
    return ToolSpec(
        name="run_command",
        description=(
            "在项目内执行命令并返回输出；支持 pytest、python 模块或脚本、只读 git、"
            "以及 uv add/remove/sync/lock（依赖安装只走声明式，会改动 pyproject.toml）。"
            "若你刚提出过尚未落盘的改动，会在试跑副本上执行，测到的就是改动后的代码"
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
        handler=lambda args: _run(root, args, pending),
    )
