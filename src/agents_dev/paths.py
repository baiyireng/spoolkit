"""项目内路径的安全解析。

所有涉及文件系统的工具都必须经由 resolve_within 取得路径，
以保证模型无法通过相对路径、绝对路径或符号链接逃逸出项目根目录。
"""

from pathlib import Path

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

