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

    pytest 用退出码区分两件完全不同的事：1 是**测试没通过**（该去改代码），
    2 是**收集中断、压根没跑成**（多半是环境问题，改代码没用）。
    把后者当成前者，模型会拿着 PermissionError 去查自己的改动——
    实测它为此烧掉好几步，最后也没查出任何东西。
    """
    first = text.splitlines()[0] if text else ""
    # 格式是「退出码 1（失败）」——数字和括号连着，别按空格切。
    match = re.match(r"退出码\s*(-?\d+)", first)
    return int(match.group(1)) if match else None

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

    def verify() -> ToolResult:
        # 按原始测试判定：模型把测试改成 `assert True` 就能骗过一次验证，
        # 然后宣布完成——实测发生过。验证不能让它自己定标准。
        revert = [change.path for change in pending.items() if is_test_path(change.path)]
        result = run_once(
            root, command, pending=pending, timeout=timeout, revert=revert
        )
        if not result.ok:
            # 失败时只留「错在哪」。整段 pytest 输出会把真正的原因淹掉。
            code = exit_code(result.content)
            detail = condense_test_output(result.content)
            if code is not None and code not in (1,):
                # 1 之外的退出码不是「你的测试挂了」，而是「测试没跑成」。
                return ToolResult(
                    ok=False,
                    content=(
                        f"测试没能跑起来（退出码 {code}，属于环境问题，"
                        f"不是你的代码失败）：{detail}。"
                        "先不要改代码——这个失败和你的改动无关。"
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
