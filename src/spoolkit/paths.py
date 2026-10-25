"""项目内路径的安全解析。

所有涉及文件系统的工具都必须经由 resolve_within 取得路径，
以保证模型无法通过相对路径、绝对路径或符号链接逃逸出项目根目录。
"""

from pathlib import Path
from typing import Sequence

from spoolkit.errors import PathOutsideProjectError
from spoolkit.prompt_sanitize import restore_temp_placeholder


def resolve_within(root: Path, candidate: str) -> Path:
    """把 candidate 解析为 root 之下的绝对路径。

    越界、空路径或无法解析的路径一律抛出 PathOutsideProjectError。
    """
    if not candidate or not candidate.strip():
        raise PathOutsideProjectError("路径为空")

    # 模型看到的提示词里临时路径被抹成了 `<临时目录>`，它照抄当路径用是
    # 合理行为——在这里认回来（fix-bug 探针：模型照抄占位符当 cwd，
    # 连撞「工作目录不存在」）。
    candidate = restore_temp_placeholder(candidate.strip())

    base = root.resolve()
    raw = Path(candidate)
    target = raw if raw.is_absolute() else base / raw
    resolved = target.resolve()

    if resolved != base and base not in resolved.parents:
        # 报错要带上「该怎么写」。实测有模型拿 `/workspace` 这种绝对路径当 cwd
        # （那是它在别处见过的约定），连续撞十几次都不换写法——因为它只知道
        # 越界了，不知道什么才算不越界。
        #
        # 「怎么才能做」有两个入口，两个都点出来：命令行一次性的 `--allow-read`，
        # 以及本机持久关联的 `spool extra add`（读放开，**写要确认**）。
        # 只说「越界了」等于把出路指到墙外。
        raise PathOutsideProjectError(
            f"路径越出项目根目录: {candidate}。"
            f"要用**相对于工作区**的路径，例如 `{base.name}/子目录` 或 `src/x.py`；"
            "要真的碰工作区外的目录，先在那台机器上关联它："
            "`spool extra add <目录>`（读放开，写要确认；聊天里加不了），"
            "或临时用 `--allow-read <目录>`。"
        )
    return resolved


def resolve_readable(
    root: Path, candidate: str, extra_roots: Sequence[Path] = ()
) -> Path:
    """读操作用的解析：项目根目录，或任一**已授权**的额外可读根之下。

    为什么不直接把工作区绑到目标目录：那是把「看一个目录」和「换个项目」
    混成一件事。工作区是身份——记忆、索引、检查点都挂在它上面——不该被
    一个临时任务顺手换掉。所以额外目录做成一份**显式授权**：读得到，
    但工作区不变，写入也仍然只在工作区内。
    """
    if not candidate or not candidate.strip():
        raise PathOutsideProjectError("路径为空")

    candidate = restore_temp_placeholder(candidate.strip())
    raw = Path(candidate)
    allowed = [root.resolve(), *(item.resolve() for item in extra_roots)]
    target = raw if raw.is_absolute() else allowed[0] / raw
    resolved = target.resolve()

    for base in allowed:
        if resolved == base or base in resolved.parents:
            return resolved

    hint = (
        "（要读工作区之外，先在那台机器上关联它：`spool extra add <目录>`"
        "——读放开，写要确认；或临时用 `--allow-read <目录>`）"
        if not extra_roots
        else f"（已授权的可读目录：{'、'.join(str(item) for item in allowed)}）"
    )
    raise PathOutsideProjectError(f"路径越出可读范围: {candidate}{hint}")

