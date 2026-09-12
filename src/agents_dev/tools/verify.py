"""自动验证：改完就替模型跑一遍项目里的测试。

模型会改错，而且看不出来自己错了。工作区里明明有测试，它却几乎从不主动
去跑——实测 25 题里 8 条失败，没有一条调用过 run_command。
指望它养成这个习惯是不现实的，那就由循环替它跑，把结果顶回去。

验证与 run_command 走同一条路径（同一套白名单、同一个试跑副本），
真实工作区全程不动。系统自己开一条绕开白名单的暗门，比模型乱跑更糟。
"""

import re
from pathlib import Path
from typing import Callable

from agents_dev.tools.edit import is_test_path
from agents_dev.tools.exec import DEFAULT_TIMEOUT, run_once
from agents_dev.tools.grant import ALLOWED, classify
from agents_dev.tools.types import ToolResult

TEST_COMMAND = ("python", "-m", "pytest", "-q")

# 环境问题的报告带这个前缀。主循环据此判断「这次失败是环境造成的」，
# 而不是去猜文案——文案会改，标记不会。
ENVIRONMENT_MARKER = "【环境问题】"

_MARKERS = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")


def condense_test_output(text: str, limit: int = 360) -> str:
    """从测试输出里挑出「错在哪」，把表头和分隔线丢掉。

    原先整段回灌，前几百字多半是分隔线、进度点和进度条，真正的报错被挤到
    后面。实测模型拿着这种反馈会去改测试文件，而不是去改被它改坏的代码。

    优先取 pytest 的短摘要（`FAILED test_x.py::test_y - AssertionError: ...`）：
    一行里同时有文件、用例名和具体差异，是最省 token 也最可行动的形式。
    """
    lines = [line.rstrip() for line in text.splitlines()]

    summary = [
        line.strip()
        for line in lines
        if line.strip().startswith(("FAILED ", "ERROR "))
    ]
    if summary:
        return " ｜ ".join(summary)[:limit]

    # 没有短摘要（比如进程直接崩了）时，退回 pytest 的 E 行
    errors = [
        line.strip()[2:].strip()
        for line in lines
        if line.strip().startswith("E ")
    ]
    errors = [item for item in errors if item]
    if errors:
        return " ｜ ".join(errors)[:limit]

    # 最后退回到去掉分隔行之后的前几行
    fallback: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or set(stripped) <= set("=_-."):
            continue
        if stripped.startswith(("退出码", "$ ")) or stripped.endswith("[100%]"):
            continue
        fallback.append(stripped)
        if len(fallback) >= 4:
            break
    return " ｜ ".join(fallback)[:limit]


def exit_code(text: str) -> int | None:
    """从「退出码 N（失败）」那一行里取出退出码。

    注意：**退出码分不出「测试没过」和「测试没跑成」**——实测收集阶段
    被权限错误打断时，pytest 返回的也是 1。所以这个函数只用于识别
    「一个测试都没收集到」（5）这类明确的信号，别指望它做更多。
    """
    first = text.splitlines()[0] if text else ""
    # 格式是「退出码 1（失败）」——数字和括号连着，别按空格切。
    match = re.match(r"退出码\s*(-?\d+)", first)
    return int(match.group(1)) if match else None


# 一看就像环境问题的异常。光有这些还不够——还得看报错指不指向工作区。
_INFRA_HINTS = (
    "INTERNALERROR",
    "PermissionError",
    "FileNotFoundError",
    "MemoryError",
    "OSError",
)

_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'()\[\],;]+")


def mentions_workspace(text: str, root: Path) -> bool:
    """报错里有没有指向工作区内的文件。"""
    try:
        base = root.resolve()
    except OSError:
        return True
    for raw in _PATH.findall(text):
        try:
            candidate = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if candidate == base or base in candidate.parents:
            return True
    return False


def looks_environmental(text: str, root: Path) -> bool:
    """这个失败看起来是不是环境造成的，而不是代码。

    判据是「报错指向哪里」：代码问题（导入失败、语法错）会指向工作区里的
    文件；环境问题（临时目录不可写、解释器坏了、缺依赖）指向工作区外面。

    这条是实战逼出来的：机器上有个坏掉的临时目录，pytest 收集阶段就崩了，
    而反馈只说「测试失败」。模型拿着 PermissionError 去查自己的改动，
    烧掉约 5 步，什么也没查出来——它没法知道那个失败与它无关。
    """
    if exit_code(text) == 5:  # 一个测试都没收集到
        return True
    if not any(hint in text for hint in _INFRA_HINTS):
        return False
    return not mentions_workspace(text, root)

def detect_test_command(root: Path) -> list[str] | None:
    """项目里看起来有测试就返回跑测试的命令，否则返回 None。

    宁可不跑也不要瞎跑：在一个没有测试的项目里凭空调 pytest，只会得到
    一条「找不到测试」的输出，然后被当成失败反馈灌回给模型。
    """
    if any((root / name).exists() for name in _MARKERS):
        return list(TEST_COMMAND)
    if (root / "tests").is_dir():
        return list(TEST_COMMAND)
    for pattern in ("test_*.py", "*_test.py"):
        if any(root.rglob(pattern)):
            return list(TEST_COMMAND)
    return None


def make_verifier(
    root: Path,
    pending,
    timeout: int = DEFAULT_TIMEOUT,
) -> Callable[[], ToolResult] | None:
    """装配一个验证器。没有测试、或命令不在白名单里，就返回 None。"""
    command = detect_test_command(root)
    if command is None:
        return None
    # 不在白名单里就不自动跑：自动验证不该顺带向用户要授权，
    # 那会把「我想安静地验一下」变成一次打断。
    if classify(command)[0] != ALLOWED:
        return None

    # 一旦确认是环境坏了，就不再重跑：结论不会变，而每跑一次都要往
    # 上下文里再灌一遍同样的报错。实测一次运行里它被重报了 7 次。
    broken = False

    def verify() -> ToolResult:
        nonlocal broken
        if broken:
            return ToolResult(
                ok=False,
                content=(
                    "测试环境依然是坏的（前面已经确认过，和你的改动无关）。"
                    "继续手头的事，不要为它改代码。"
                ),
            )
        # 按原始测试判定：模型把测试改成 `assert True` 就能骗过一次验证，
        # 然后宣布完成——实测发生过。验证不能让它自己定标准。
        revert = [change.path for change in pending.items() if is_test_path(change.path)]
        result = run_once(
            root, command, pending=pending, timeout=timeout, revert=revert
        )
        if not result.ok:
            # 失败时只留「错在哪」。整段 pytest 输出会把真正的原因淹掉。
            detail = condense_test_output(result.content)
            if looks_environmental(result.content, root):
                broken = True
                return ToolResult(
                    ok=False,
                    content=(
                        f"{ENVIRONMENT_MARKER}测试没能跑起来"
                        f"（报错指向的是工作区外面的东西，"
                        f"属于环境问题）：{detail}。"
                        "这不是你的代码造成的，不要为它改代码——"
                        "把情况说清楚就行；如果确实需要查清是什么坏了，"
                        "用 request_diagnosis 登记，让具备真实环境权限的会话去验。"
                    ),
                )
            return ToolResult(
                ok=False,
                content=(
                    f"测试没有通过：{detail}。"
                    "按这个报错改代码——不要改测试文件。"
                ),
            )
        if revert:
            return ToolResult(
                ok=True,
                content=(
                    result.content
                    + f"（注意：你改过 {len(revert)} 个测试文件，"
                    "验证是按它们原来的内容判定的）"
                ),
            )
        return result

    return verify
