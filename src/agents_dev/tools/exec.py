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
from agents_dev.tools.grant import ALLOWED, ASK, DENIED, Grants, classify

# 60 秒对真实测试套件是不够的：这个项目自己的套件跑一次就要十几秒，
# 加上试跑副本的开销很容易翻倍。超时太小会逼着模型去瞎摸索，
# 把步数预算浪费在恢复上，而不是任务本身。
DEFAULT_TIMEOUT = 180
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


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    # 上限是能力标定值：小模型上 4000 字符够用，强模型读得下更多——
    # 硬截断会把「失败在哪」直接切掉。
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        f"{text[:half]}\n"
        f"…（输出过长，中间省略 {len(text) - limit} 字符）…\n"
        f"{text[-half:]}"
    )


def _execute(
    base: Path,
    argv: list[str],
    requested: str,
    cwd_arg: str,
    timeout: int,
    max_output: int = MAX_OUTPUT_CHARS,
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

    body = _truncate(
        ((proc.stdout or "") + (proc.stderr or "")).strip())
    status = "成功" if proc.returncode == 0 else "失败"
    # 输出放在前面：模型和人都先要知道「错在哪」，
    # 而不是先看到自己刚才发了什么命令。
    return ToolResult(
        ok=proc.returncode == 0,
        content=f"退出码 {proc.returncode}（{status}）\n$ {requested}\n{body}",
    )


def _request_approval(
    argv: list[str], reason: str, approver, grants: Grants
) -> tuple[bool, str]:
    """向用户申请执行权限。返回（是否获批，说明）。"""
    if grants.allows(argv):
        return True, ""
    if approver is None:
        return False, (
            f"这条命令不在白名单内，需要用户批准：{reason}\n"
            "当前无人可询问（无人值守运行）。如果它确实必要，"
            "请在结论里说明需要用户手动执行什么。"
        )

    answer = approver(argv, reason)
    if answer == "once":
        return True, ""  # 只放这一次，不记
    if answer in ("session", "always"):
        grants.grant(argv, permanent=answer == "always")
        return True, ""
    if answer == "block":
        grants.block(argv)
        return False, (
            f"用户拒绝了，并且本轮不再询问这类命令：{' '.join(argv)}。"
            "换个不依赖它的做法；确实必要就在结论里说明需要用户手动执行什么。"
        )
    return False, f"用户拒绝了执行：{' '.join(argv)}"


def _shape_problem(argv: list[str]) -> str | None:
    """命令是不是被整条塞进了第一个元素里。

    实测这是一类很贵的错误：模型把 `python -m pytest a/b.py -q` 当成**一个**
    字符串放进数组，白名单于是把它当成一个叫「python -m pytest a/b.py -q」的
    程序，报「不在白名单内，需要用户批准」。**理由完全指错了方向**——它据此
    去申请权限，无人值守时没人可问，于是卡死（实测烧掉三步，转而自己写脚本，
    最后撞上输出预算收尾）。

    形状问题就该说形状。只查第一个元素：程序名里不可能有空格，而后面几项
    有空格是合法的（比如 pytest 的 `-k "a and b"`）。
    """
    program = argv[0].strip()
    if " " not in program:
        return None
    return (
        "command 要**拆成数组**，每个参数一个元素——现在整条命令是一个元素"
        f"（{program!r}），它被当成了一个可执行文件名。\n"
        '正确写法：["python", "-m", "pytest", "a/b.py", "-q"]'
    )


# 这些不是「没权限」，是**这里不需要**。混进权限措辞会把它引去申请授权，
# 而正确做法它猜不到。
SHELL_BUILTINS = ("cd", "chdir", "export", "set", "source", "&&", "|")


def _usage_problem(argv: list[str]) -> str | None:
    """用法问题——不是权限问题。先说清该怎么写，别让它去申请权限。

    实测：它写 `cd` 换目录（或 `cd x && pytest` 那一套），撞上「不在白名单内」
    之后只会换个写法再撞，连撞十几次都不换思路——因为它只知道「不行」，
    不知道「该怎么写」。换目录的正确做法是 cwd 参数。
    """
    problem = _shape_problem(argv)
    if problem is not None:
        return problem
    program = argv[0].strip().lower()
    if program in SHELL_BUILTINS:
        return (
            f"`{program}` 不用写：这里没有 shell，也没有授权一说。"
            "换目录请用 cwd 参数（相对工作区的路径，例如 "
            '"cwd": "13_case_insensitive"）；串联命令请拆成多次调用。'
        )
    return None


def _run(
    root: Path,
    args: dict,
    pending=None,
    approver=None,
    grants: Grants | None = None,
    revert: object = (),
    max_output: int = MAX_OUTPUT_CHARS,
) -> ToolResult:
    argv = list(args["command"])
    usage = _usage_problem(argv)
    if usage is not None:
        return ToolResult(ok=False, content=usage)
    level, reason = classify(argv)
    if level == DENIED:
        return ToolResult(ok=False, content=reason)
    if level == ASK:
        store = grants if grants is not None else Grants()
        if store.blocks(argv):
            return ToolResult(
                ok=False,
                content=(
                    f"用户在本轮已经表过态：这类命令统统不许。"
                    f"（{' '.join(argv)}）换个不依赖它的做法，或者"
                    "在结论里说明需要用户手动执行什么。"
                ),
            )
        allowed, message = _request_approval(argv, reason, approver, store)
        if not allowed:
            return ToolResult(ok=False, content=message)

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
            _restore_originals(work_root, pending, revert)
            return _execute(
                work_root, resolved, requested, cwd_arg, timeout, max_output
            )
    return _execute(root, resolved, requested, cwd_arg, timeout, max_output)


def _restore_originals(work_root: Path, pending, paths: object) -> None:
    """把指定路径在试跑副本里还原成改动之前的样子。

    给自动验证用：模型把测试文件改成 `assert True` 就能骗过验证，
    然后理直气壮地宣布完成——实测发生过。验证必须按原始测试判定，
    否则它测的是模型希望看到的结论，而不是代码的真实表现。
    """
    wanted = set(paths or ())
    if not wanted:
        return
    for change in pending.items():
        if change.path not in wanted:
            continue
        target = work_root / change.path
        if change.is_new_file:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.old_text, encoding="utf-8")


def run_command_spec(
    root: Path,
    pending=None,
    approver=None,
    grants: Grants | None = None,
    max_output: int = MAX_OUTPUT_CHARS,
) -> ToolSpec:
    """执行白名单命令。有未落盘改动时自动在试跑副本上执行。"""
    return ToolSpec(
        name="run_command",
        description=(
            "在项目内执行命令并返回输出；支持 pytest、python 模块或脚本、只读 git、"
            "以及 uv add/remove/sync/lock（依赖安装只走声明式，会改动 pyproject.toml）。"
            "若你刚提出过尚未落盘的改动，会在试跑副本上执行，测到的就是改动后的代码。"
            "跑测试优先只跑相关文件，全量套件慢且容易超时；确需更长时间就调大 timeout"
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
        handler=lambda args: _run(
            root, args, pending, approver, grants, max_output=max_output
        ),
        brief="跑测试或脚本",
        group="跑",
    )


def run_once(
    root: Path,
    argv: list[str],
    pending=None,
    approver=None,
    grants: Grants | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    revert: object = (),
    cwd: str = ".",
    max_output: int = MAX_OUTPUT_CHARS,
) -> ToolResult:
    """执行一条命令，语义与 run_command 工具完全一致。

    给自动验证用：它必须走同一套白名单与试跑副本，否则「模型不能随便
    跑命令、但系统可以」就成了一条暗门——而暗门是最难审计的那种东西。
    """
    return _run(
        root,
        {"command": list(argv), "cwd": cwd, "timeout": timeout},
        pending,
        approver,
        grants,
        revert,
        max_output,
    )
