"""自动验证：改完就替模型跑一遍项目里的测试。

模型会改错，而且看不出来自己错了。工作区里明明有测试，它却几乎从不主动
去跑——实测 25 题里 8 条失败，没有一条调用过 run_command。
指望它养成这个习惯是不现实的，那就由循环替它跑，把结果顶回去。

验证与 run_command 走同一条路径（同一套白名单、同一个试跑副本），
真实工作区全程不动。系统自己开一条绕开白名单的暗门，比模型乱跑更糟。
"""

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
            return ToolResult(
                ok=False, content=condense_test_output(result.content)
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
