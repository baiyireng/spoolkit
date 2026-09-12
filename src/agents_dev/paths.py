"""项目内路径的安全解析。

所有涉及文件系统的工具都必须经由 resolve_within 取得路径，
以保证模型无法通过相对路径、绝对路径或符号链接逃逸出项目根目录。
"""

from pathlib import Path
from typing import Sequence

from agents_dev.errors import PathOutsideProjectError


def resolve_within(root: Path, candidate: str) -> Path:
    """把 candidate 解析为 root 之下的绝对路径。

    越界、空路径或无法解析的路径一律抛出 PathOutsideProjectError。
    """
    if not candidate or not candidate.strip():
        raise PathOutsideProjectError("路径为空")

    base = root.resolve()
    raw = Path(candidate)
    target = raw if raw.is_absolute() else base / raw
    resolved = target.resolve()

    if resolved != base and base not in resolved.parents:
        raise PathOutsideProjectError(f"路径越出项目根目录: {candidate}")
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

    raw = Path(candidate)
    allowed = [root.resolve(), *(item.resolve() for item in extra_roots)]
    target = raw if raw.is_absolute() else allowed[0] / raw
    resolved = target.resolve()

    for base in allowed:
        if resolved == base or base in resolved.parents:
            return resolved

    hint = (
        "（要读工作区之外，先显式授权：加 --allow-read <目录>）"
        if not extra_roots
        else f"（已授权的可读目录：{'、'.join(str(item) for item in allowed)}）"
    )
    raise PathOutsideProjectError(f"路径越出可读范围: {candidate}{hint}")

