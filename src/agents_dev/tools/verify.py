"""自动验证：改完就替模型跑一遍项目里的测试。

模型会改错，而且看不出来自己错了。工作区里明明有测试，它却几乎从不主动
去跑——实测 25 题里 8 条失败，没有一条调用过 run_command。
指望它养成这个习惯是不现实的，那就由循环替它跑，把结果顶回去。

验证与 run_command 走同一条路径（同一套白名单、同一个试跑副本），
真实工作区全程不动。系统自己开一条绕开白名单的暗门，比模型乱跑更糟。
"""

from pathlib import Path, PurePosixPath
from typing import Callable

from agents_dev.tools.exec import DEFAULT_TIMEOUT, run_once
from agents_dev.tools.grant import ALLOWED, classify
from agents_dev.tools.types import ToolResult

TEST_COMMAND = ("python", "-m", "pytest", "-q")

_MARKERS = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")

_TEST_DIRS = ("test", "tests")


def is_test_path(path: str) -> bool:
    """这个路径看起来是不是测试文件。"""
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return False
    if pure.name.startswith("test_") or pure.name.endswith("_test.py"):
        return True
    return any(part.lower() in _TEST_DIRS for part in pure.parts[:-1])


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
        if revert and result.ok:
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
