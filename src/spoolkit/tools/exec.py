"""命令执行工具。

这是「能改代码」和「能验证代码」之间的那道坎。没有它，agent 只能写出
看起来对的代码。

安全边界靠四条：
1. **白名单命令**，不做任意 shell 透传；
2. **不经过 shell**，参数里的 `;`、`|`、`&&` 都是普通字符；
   （唯一的例外是 Windows 上解析到 `.cmd`/`.bat` 垫片的命令——那种只能经
   `cmd.exe` 起，见 `resolve_argv`：那时参数里的 `& | < > ^ % "` 一律
   **明确拒绝并说人话**，绝不悄悄拼进命令行。）
3. **工作目录锁在项目内**；
4. **超时强杀 + 输出截断**——跑不完或吐出海量日志的命令会直接吃掉上下文。

刻意禁掉 `python -c` 与 `python -i`：它们是任意代码执行的入口，
而白名单的意义正在于排除这一类。

还有一个容易被忽略的责任：**解释器由工具解析，不由模型猜**。
裸 `python`/`python3`/`pytest` 在不同机器上可能是坏的垫片、不在 PATH、
或指向错误的虚拟环境。实测中模型为此白白烧掉了十步。
"""

import os
import shutil
import subprocess
import re
from pathlib import Path

from spoolkit.errors import PathOutsideProjectError
from spoolkit.interpreter import (
    PYTHON_PROGRAMS,
    resolve_python_argv,
    spoolkit_checkout_root,
)
from spoolkit.paths import resolve_within
from spoolkit.tools.trial import trial_workspace
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit import limits as _limits
from spoolkit.tools.grant import ALLOWED, ASK, DENIED, Grants, classify

# 60 秒对真实测试套件是不够的：这个项目自己的套件跑一次就要十几秒，
# 加上试跑副本的开销很容易翻倍。超时太小会逼着模型去瞎摸索，
# 把步数预算浪费在恢复上，而不是任务本身。
# 默认值来自登记表；名字留着给外部导入。
DEFAULT_TIMEOUT = int(_limits.knob("command_timeout").default)
MAX_TIMEOUT = int(_limits.knob("max_command_timeout").default)
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


# --- Windows 上的 `.cmd`/`.bat` 垫片：能起，但不许经它偷渡 shell 元字符 --------
#
# 背景（一条被误判过很久的"环境债"）：Windows 上 `subprocess` 只按 PATH 找
# `.exe`/`.com`，而 `node`、`pnpm`、`ruff` 这类工具在 nvm-for-windows / volta /
# 本机 DSH 下都是 `.cmd` 垫片。直接 spawn 会报「找不到命令: <名字>」——**而它
# 明明装着**，报错还把人往"没装"的方向带。得包成 `cmd /c call <垫片> …` 才起得来。
#
# 代价是 `cmd.exe` 会把命令行重新解释一遍。所以这里有两条硬规矩：
# ① 参数**逐字**传（仍是 argv 数组，引用交给 `subprocess.list2cmdline`：
#    不做任何字符串拼接，也就不存在"拼错一个引号、参数被切开"这回事）；
# ② 参数里出现 `cmd` 会重新解释的字符就**明确拒绝**，不猜、不转义、不静默丢。

#: `cmd.exe` 会重新解释的字符：`&` 分句、`|`/`<`/`>` 转向、`^` 转义、`%` 展开。
#: 直接 spawn 一个 `.exe` 时它们都是普通字符（`shell=False`），经 `cmd.exe` 就不是了。
CMD_METACHARS = "&|<>^%"

#: 双引号单独说：`list2cmdline` 用 `\"` 转义它，而 `cmd.exe` **不认反斜杠转义**
#: ——`a"b` 会被切成两个参数。既然这条路保证不了"逐字到达"，就不放行。
#: 何况参数本来就是数组里的一项，不需要自己加引号。
CMD_QUOTE = '"'


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

    # node --check：只允许语法检查，不允许任意脚本执行。
    # 验证器用它做 HTML 内联 JS 的降级语法检查（dogfood：贪吃蛇写完没有验证）。
    # dogfood 第二轮：agent 直接对 HTML 文件跑 node --check，报 ERR_UNKNOWN_FILE_EXTENSION。
    # 限制文件后缀为 .js——HTML 要先提取 JS 再检查（验证器会自动做）。
    if program == "node" or program.endswith("node.exe"):
        if len(rest) >= 2 and rest[0] == "--check":
            target = rest[1]
            if not target.lower().endswith(".js"):
                return (
                    "node --check 只接受 .js 文件。HTML 文件不能直接检查——"
                    "系统会自动从 HTML 里提取内联 JS 做语法检查，你不需要手动做。"
                )
            # 目标路径里不许有 shell 元字符。原因很具体：Windows 上 node 常常是
            # `.cmd` 垫片（nvm/volta/pnpm/本机 DSH 都是），`resolve_argv` 得把它包成
            # `cmd /c call <垫片> …` 才起得来，而 `cmd` 会把 `& | ^ < > %` 重新当元字符。
            # 不收这一条，包装就等于在本模块"不经过 shell"的边界上开了个口子。
            # （`resolve_argv` 那道口子上还有同一套检查兜着，见 `CMD_METACHARS`；
            # 这里是更早、更具体的一层：它连"要不要批准"都不用问。）
            if any(ch in target for ch in CMD_METACHARS):
                return (
                    "node --check 的目标路径里不能有 shell 元字符（& | ^ < > %）："
                    "Windows 上 node 可能是 `.cmd` 垫片，得经 cmd.exe 起它，"
                    "而那些字符在 cmd 里有特殊含义。换成普通文件名。"
                )
            return None
        return "node 只允许 --check（语法检查），不允许执行脚本"

    return f"命令不在白名单内: {program}"


class CmdShimRejected(RuntimeError):
    """这条命令只能经 `cmd.exe` 起（`.cmd`/`.bat` 垫片），而参数里有 cmd 会重新
    解释的字符。消息就是给人看的那句话，`_run` 原样交给模型。"""


def _cmd_shim(program: str) -> str | None:
    """这个程序名在本机解析到的 `.cmd`/`.bat` 垫片；不是垫片就 None。

    判据只有"解析到的文件后缀"这一条，**不挑程序名**：`node`、`pnpm`、`ruff.cmd`
    在 Windows 上是同一类东西，没有理由只给其中一个开这条路。
    `.exe`/`.com` 返回 None——它们直接 spawn 就行，多包一层 `cmd` 只会多一处
    引号与元字符的坑。
    """
    if os.name != "nt":
        return None
    found = shutil.which(program)
    if not found:
        return None
    return found if Path(found).suffix.lower() in (".cmd", ".bat") else None


def _shim_problem(shim: str, argv: list[str]) -> str | None:
    """参数（含垫片路径本身）里有没有"经 cmd 就不是原样"的字符。

    返回给人看的那句话；干净就 None。要点三样：**为什么**（这条命令得经
    `cmd.exe`）、**哪一个值**出问题、**怎么办**（去掉字符，而不是让它反复试）。
    """
    offenders: list[str] = []
    labelled = [("垫片路径", shim)]
    labelled += [(f"参数 {i}", arg) for i, arg in enumerate(argv[1:], 1)]
    for label, value in labelled:
        bad = [ch for ch in CMD_METACHARS if ch in value]
        if bad:
            offenders.append(f"{label} {value!r}（含 {' '.join(bad)}）")
        elif CMD_QUOTE in value:
            offenders.append(f"{label} {value!r}（含双引号）")
    if not offenders:
        return None
    return (
        "这条命令在本机解析到的是 `.cmd`/`.bat` 垫片，Windows 上只能经 cmd.exe 起它：\n"
        f"  {shim}\n"
        "而 cmd.exe 会把命令行里的 & | < > ^ % 重新解释（分句、转向、转义、展开），"
        '双引号也会打乱它的引用规则——参数就不是原来那个了，所以这里明确不跑，'
        "也不替你转义（转义过的参数到底变成什么，没法保证）：\n"
        "  " + "\n  ".join(offenders) + "\n"
        "怎么办：把这些字符从参数里去掉。换目录请用 cwd 参数，串联命令请拆成多次调用；"
        "参数本来就是数组里的一项，不需要自己加引号。"
        "确实必须原样执行这条命令时，请让用户手动跑。"
    )


def resolve_argv(argv: list[str], root: Path) -> list[str]:
    """把模型写的命令名换算成本机真实可用的可执行文件。

    模型不该知道也不该猜「用哪个解释器」——那是环境细节，不是任务信息。

    候选根按优先级排：**① 工作区 `.venv` → ② spoolkit 检出根 `.venv` →
    ③ `sys.executable`**。选择与映射的实现都不在这里，在 `spoolkit.interpreter`
    （bench 的验收命令问的是同一个问题，只是第一个候选根换成任务工作区）；
    这一处只交出自己那两份候选。顺序即优先级，不能调换。

    **为什么要有第二个候选根（代价与边界，写在这里免得以后有人惊讶）**：
    第一候选仍是项目自带的 `.venv`，所以**正常项目不受影响**——它自己的环境
    永远排在前面。只有当工作区**没有** `.venv` 时，才会退回**检出目录**的
    `.venv`（那里有 spoolkit 自己的 dev 依赖，包括 pytest），而**不是**
    `sys.executable`。这是有意的：agent 的自动验证/自测要和「跑 spool 的那套
    环境」解耦。spool 由 uv tool / pipx / 系统 Python 启动时，`sys.executable`
    里通常没有 pytest，于是 agent 每次自测都撞 `No module named pytest`——
    判定没错，但轨迹和 token 被污染，模型还会去「修」一个它修不了的环境问题。
    **代价**：用户工作区里跑 `python x.py`，若这个脚本依赖项目独有的第三方包、
    而工作区**又没有** `.venv`，它可能拿到检出环境的解释器，而不是 spool 环境的。
    这种情况本来就拿不到 spool 环境的包（spool 环境里也没有该项目的依赖），
    但依赖「恰好装在 spool 那个解释器里」的脚本会受影响——要消除这个歧义，
    给工作区建 `.venv` 即可。

    **Windows 上的 `.cmd`/`.bat` 垫片（对任意命令生效）**：`node`、`pnpm`、
    `ruff` 这类命令常常不是 `.exe`，而是一个 `.cmd` 垫片（nvm-for-windows、
    volta、pnpm 装的都是；本机 DSH 提供的 node 也是
    `ELECTRON_RUN_AS_NODE=1 "…DeepSeek Harness.exe"`）。`subprocess` 在 Windows 上
    只按 `PATH` 找 `.exe`/`.com`，于是直接 spawn 会报「找不到命令: <名字>」——
    **而那个命令明明装着**，报错还把人往"没装"的方向带。这里把它包成
    `cmd /c call <垫片> …`（只有这一种形状）。

    为什么是 `cmd /c call …` 而不是 `cmd /c …`（实测四种形状，只有它稳）：
    `cmd /c "C:\\Program Files\\x\\node.cmd" a b` 会命中 cmd 自己的引号剥离规则
    ——命令行上出现两对以上引号时，它把**首尾各一个**引号去掉，路径就从空格处
    断成两截，报的是"`C:\\Program Files\\x\\shim` 不是内部或外部命令"。加一个
    `call`（它是 cmd 内建，不多起进程）之后，cmd 不再对 `/c` 之后的部分做那套
    剥离，引号原样交给批处理；实测带空格路径 + 带空格/中文参数逐字到达，
    退出码也照原样传出来（`exit /b 3` → 3）。

    为什么敢经 `cmd.exe`（本模块自述的边界之一是"不经过 shell"）：
    **参数逐字进、元字符一律挡在门外。**
    - 逐字：仍然是 argv 数组，一个参数一个元素，引用交给
      `subprocess.list2cmdline`（`shell=False` 时它就是这条命令的引用实现）——
      这里**不做字符串拼接**，所以不存在"引号拼错、参数被切开"这回事。
    - 挡在门外：解析到垫片、且参数（或垫片路径）里有 `& | < > ^ % "` 时，
      抛 `CmdShimRejected` 并给出人话（见 `_shim_problem`），**不转义、不猜**。
      经 `cmd.exe` 的转义结果没法保证，与其悄悄改写参数，不如明确拒绝。

    其余的换算维持原样：`.exe`/`.com` 不包 `cmd`；`python`/`python3`/`pytest`
    先走解释器换算（那条路不经过这里）；不是垫片、也不在白名单里的命令原样返回，
    找不到就是找不到。
    """
    if not argv:
        return argv
    if argv[0] in PYTHON_PROGRAMS:
        return resolve_python_argv(argv, [root, spoolkit_checkout_root()])

    shim = _cmd_shim(argv[0])
    if shim is None:
        return argv
    problem = _shim_problem(shim, argv)
    if problem is not None:
        raise CmdShimRejected(problem)
    return ["cmd", "/c", "call", shim, *argv[1:]]


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


def _translate_cd(argv: list[str]) -> tuple[str, list[str]] | None:
    """把 `cd X && <命令>` 翻译成「在 X 里执行 <命令>」。

    这是**翻译，不是放行**：后面的命令仍走同一套白名单与授权，`cd` 本身
    没有任何权限含义。实测模型会反复用 shell 写法（`["cd","X","&&","python",...]`），
    即使报错已经告诉它该用 cwd——它在别处学到的习惯比提示词强。

    代价很实在：一次这样的翻车（连撞三次 + 重试）能吃掉整轮 27% 的 token。
    与其继续提醒，不如把它的意思翻对。
    """
    if len(argv) < 4:
        return None
    if argv[0].strip().lower() not in ("cd", "chdir"):
        return None
    if argv[2].strip() not in ("&&", ";", "&"):
        return None
    target, rest = argv[1].strip(), argv[3:]
    if not target or not rest:
        return None
    if target in (".", "./") or target.startswith(("-", "/", "\\")) or ":" in target:
        return None  # 相对目录之外的形式不猜
    return target, list(rest)


def _run(
    root: Path,
    args: dict,
    pending=None,
    approver=None,
    grants: Grants | None = None,
    revert: object = (),
    max_output: int = MAX_OUTPUT_CHARS,
    default_timeout: int = DEFAULT_TIMEOUT,
    max_timeout: int = MAX_TIMEOUT,
) -> ToolResult:
    argv = list(args["command"])
    original = list(argv)
    translated = _translate_cd(argv)
    if translated is not None:
        target, rest = translated
        args = {**args, "command": rest, "cwd": target}
        argv = rest
    usage = _usage_problem(argv)
    if usage is not None:
        return ToolResult(ok=False, content=usage)
    level, reason = classify(argv)
    if level == DENIED:
        return ToolResult(ok=False, content=reason)

    # 换算（解释器 / `.cmd` 垫片）**在申请授权之前**做完：一条注定起不来的命令
    # 不该先去打扰人。垫片加元字符的那条拒绝在这里落地——归因要说对，它不是
    # "没权限"（那会把它引去申请授权），而是"这条路走不通，换写法"。
    try:
        resolved = resolve_argv(argv, root)
    except CmdShimRejected as exc:
        return ToolResult(ok=False, content=str(exc))

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

    # 回显**模型自己写的那份**命令：被翻译过的时候，它要能看到「我要的」和
    # 「实际跑的」对应得上；而换算后的解释器全路径不能暴露（实测模型会把它
    # 当成参数再传回来，演变成 "can't open file <解释器路径>"）。
    requested = " ".join(original)

    timeout = args.get("timeout", default_timeout)
    if not 1 <= timeout <= max_timeout:
        return ToolResult(
            ok=False, content=f"timeout 必须在 1 到 {max_timeout} 秒之间"
        )

    if argv[0] == "uv" and shutil.which("uv") is None:
        return ToolResult(
            ok=False,
            content="未找到 uv，无法执行该命令。可以直接告诉用户需要手动安装依赖。",
        )

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
        if Path(change.path).is_absolute():
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
    default_timeout: int = DEFAULT_TIMEOUT,
    max_timeout: int = MAX_TIMEOUT,
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
            root,
            args,
            pending,
            approver,
            grants,
            max_output=max_output,
            default_timeout=default_timeout,
            max_timeout=max_timeout,
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
