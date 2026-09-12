"""命令授权分级。

白名单之外不该一刀切拒绝。一刀切的结果是模型撞到边界就无所适从——
它只能反复试、瞎摸索，把步数预算烧在恢复上，而不是任务本身。

但「都能申请」也不行：如果申请能绕过禁止清单，禁止清单就没有意义。
所以分三级：

- **allowed**：白名单内，直接执行；
- **denied**：明确禁止，永久拒绝，模型不能申请——这里放的是不可逆或
  影响面超出项目的操作，比如递归删除、重写版本历史、提权、系统级改动；
- **ask**：白名单之外但不在禁止清单里，向用户申请，批准后可执行。

分级的意义是把判断权交还给用户，同时保证危险操作不会被「申请」这条路绕过去。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

ALLOWED = "allowed"
DENIED = "denied"
ASK = "ask"

# 永久禁止：影响面明确超出项目，或本身就是提权/任意代码执行的入口。
# 这些不提供申请入口——不是因为危险就不给，而是因为它们跟「完成用户
# 交代的任务」没有正当关系。
DENIED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("shutdown",),
    ("reboot",),
    ("format",),
    ("diskpart",),
    ("reg",),
    ("sc",),
    ("schtasks",),
    ("takeown",),
    ("icacls",),
    ("net",),
    ("sudo",),
    ("runas",),
    # 下载即执行：依赖应当走 uv add，agent 没有别的理由联网取文件
    ("curl",),
    ("wget",),
    ("iwr",),
    ("invoke-webrequest",),
)

# 删除类命令**可以申请**。
#
# 原先它们一律永久拒绝。那是把「危险」和「不可申请」划了等号——结果是
# 一次误判就留下死路：后续任务确实需要删一个文件时，连问都问不到。
# 危险的东西该让人来判，而不是让工具替他判不了。
#
# 它们仍然需要申请（不自动放行），而且审批时给四档选择。
DELETE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("rm",),
    ("del",),
    ("erase",),
    ("rmdir",),
    ("rd",),
    ("remove-item",),
)

# git 的破坏性操作单独列出：它们会不可逆地丢掉版本历史，
# 而版本历史是最后的退路。
DENIED_GIT = ("reset", "clean", "push", "checkout", "rebase", "restore", "rm")


def _matches(argv: Sequence[str], prefix: Sequence[str]) -> bool:
    if len(argv) < len(prefix):
        return False
    for index, part in enumerate(prefix):
        candidate = argv[index].lower()
        if index == 0:
            # 程序名可能带完整路径和扩展名（C:\...\format.com），
            # 都要归一化之后再比对，否则禁止清单会被绕过去。
            if _program_name(candidate) != part.lower():
                return False
        elif candidate != part.lower():
            return False
    return True


_EXEC_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1")


def _program_name(value: str) -> str:
    name = Path(value).name.lower()
    for suffix in _EXEC_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def classify(argv: Sequence[str]) -> tuple[str, str]:
    """判定一条命令属于哪一级。返回（级别，说明）。"""
    if not argv:
        return DENIED, "命令不能为空"

    for prefix in DENIED_PREFIXES:
        if _matches(argv, prefix):
            return DENIED, (
                f"{' '.join(prefix)} 属于永久禁止的操作（不可逆或影响超出项目），"
                "不提供申请入口。请改用项目内可逆的方式。"
            )

    if argv[0].lower() in ("git",) and len(argv) > 1:
        if argv[1].lower() in DENIED_GIT:
            return DENIED, (
                f"git {argv[1]} 会改写或丢弃版本历史，而版本历史是最后的退路。"
                "提交与回滚请交给用户执行。"
            )

    for prefix in DELETE_PREFIXES:
        if _matches(argv, prefix):
            return ASK, (
                f"{' '.join(prefix)} 会删除文件，而且删掉就找不回来——"
                "本项目对写操作有基线可回滚，对删除没有。需要你批准；"
                "批准时可以只放这一次、始终允许（本工作区）、或者本轮一律拒绝。"
            )

    from agents_dev.tools.exec import validate_command

    reason = validate_command(list(argv))
    if reason is None:
        return ALLOWED, ""
    return ASK, reason


def key_of(argv: Sequence[str]) -> str:
    """把命令压成用于记忆授权的键：程序名加子命令。"""
    if not argv:
        return ""
    if len(argv) > 1 and not argv[1].startswith("-"):
        return f"{Path(argv[0]).name} {argv[1]}"
    return Path(argv[0]).name


@dataclass
class Grants:
    """已获得的授权。

    三层：本轮的临时允许、落盘的长期允许（绑定当前工作区）、
    以及**本轮一律拒绝**。

    最后那一层是「本轮会话全部拒绝」的落点。它必须存在，而且要按前缀粒度：
    用户拒绝一次删除，不该被记成「永久不许删」——那是死路；但也不该被
    记成「每次都要重问一遍」——那会变成骚扰。所以它只封本轮。
    """

    path: Path | None = None
    session: set[str] = field(default_factory=set)
    persistent: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.persistent = set(data.get("prefixes", []))
            except (json.JSONDecodeError, AttributeError):
                self.persistent = set()

    def allows(self, argv: Sequence[str]) -> bool:
        key = key_of(argv)
        return key in self.session or key in self.persistent

    def blocks(self, argv: Sequence[str]) -> bool:
        """本轮已表态「这类别再问了」。"""
        return key_of(argv) in self.blocked

    def block(self, argv: Sequence[str]) -> str:
        key = key_of(argv)
        self.blocked.add(key)
        return key

    def grant(self, argv: Sequence[str], permanent: bool = False) -> str:
        key = key_of(argv)
        self.blocked.discard(key)  # 允许了就不再拦
        if permanent:
            self.persistent.add(key)
            self._flush()
        else:
            self.session.add(key)
        return key

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"prefixes": sorted(self.persistent)}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
