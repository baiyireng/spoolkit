"""读操作的统一视图：待确认的改动优先于磁盘。

没有这一层时，模型活在两套互相矛盾的世界里：

- 它刚写完一个文件，`read_file` 却给它**旧内容**（改动还没落盘）；
- 同一个文件，`run_command` 在试跑副本里看到的是**新内容**；
- 索引工具查不到刚加进去的符号，于是报「找不到符号」。

实测的后果很具体：审查者读完磁盘后判定「三个文件均未做任何修改」，
把一处完全正确的实现判成了失败。它反复翻文件，多半就是在试图调和
这个矛盾。

`trial_workspace` 的设计意图是「模型以为改了，那就让它在试跑环境里
真的改了」——这个意图原先只落实在命令执行上，现在读操作也跟上。
"""

from pathlib import Path


class WorkspaceView:
    """把待确认改动叠加在真实文件之上的一层只读视图。

    刻意**不缓存** pending 的内容：工具规格是在装配时构造一次的，
    如果那时就把改动快照下来，之后的每一次写入它都看不见——
    表现是「模型刚写完，读回来还是空的」，而且极难往这上面想。
    """

    def __init__(self, root: Path, pending=None) -> None:
        self.root = root.resolve()
        self._pending = pending

    def _overlay(self) -> dict[str, str]:
        if self._pending is None:
            return {}
        return {change.path: change.new_text for change in self._pending.items()}

    def _abs_overlay(self) -> dict[Path, str]:
        """待确认改动按磁盘绝对路径索引，工作区和关联目录都能对上。"""
        if self._pending is None:
            return {}
        out: dict[Path, str] = {}
        disk = getattr(self._pending, "disk_path", None)
        for change in self._pending.items():
            target = (
                disk(change.path)
                if callable(disk)
                else (self.root / change.path)
            )
            out[target.resolve()] = change.new_text
        return out

    # --- 查询 ---

    def relative(self, target: Path) -> str:
        """绝对路径 → 项目内相对路径（POSIX 形式）。

        工作区之外的路径（关联目录）返回空串：索引那套只认工作区内相对路径。
        """
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            return ""

    def is_overridden(self, relative: str) -> bool:
        return relative in self._overlay()

    def overridden_text(self, relative: str) -> str | None:
        """这个路径在待确认改动里的内容；没被改过则返回 None。"""
        return self._overlay().get(relative)

    @property
    def overridden(self) -> tuple[str, ...]:
        return tuple(self._overlay())

    def _deleted(self) -> set[Path]:
        """待确认改动里那几处**删除**：它们已不存在，但磁盘上还在。"""
        if self._pending is None:
            return set()
        disk = getattr(self._pending, "disk_path", None)
        return {
            (disk(change.path) if callable(disk) else (self.root / change.path)).resolve()
            for change in self._pending.items()
            if getattr(change, "deleted", False)
        }

    def deleted_paths(self) -> set[Path]:
        """哪几个绝对路径在待确认改动里被删了（读工具据此不再给人看）。"""
        return self._deleted()

    def exists(self, target: Path) -> bool:
        """这个路径「现在」存在吗——叠加待确认改动之后的事实。

        删除先判：磁盘上它还在（改动没落盘），但在模型该看到的世界里
        它已经不在了。
        """
        resolved = target.resolve()
        if resolved in self._deleted():
            return False
        return resolved in self._abs_overlay() or target.exists()

    def is_dir(self, target: Path) -> bool:
        # 待确认的改动只可能是文件，不会凭空造出目录。
        return target.is_dir()

    def read_text(self, target: Path) -> str:
        """读文件内容。改过的文件给改之后的内容。

        删除不在这条路径上给内容：调用方必须能分清「读到一个文件」与
        「这个文件已经被删掉了」，否则 `read_file` 会把旧内容端给模型，
        而模型据此以为自己没删掉。
        """
        overlay = self._abs_overlay()
        resolved = target.resolve()
        if resolved in overlay:
            if resolved in self._deleted():
                raise FileNotFoundError(f"{target} 已在待确认改动里被删除")
            return overlay[resolved]
        return target.read_text(encoding="utf-8")

    def overlay_children(self, directory: Path) -> list[str]:
        """这个目录下有哪些文件只存在于待确认改动里（磁盘上还没有）。"""
        names = set()
        base = directory.resolve()
        for path in self._abs_overlay():
            try:
                rest = path.relative_to(base)
            except ValueError:
                continue
            if len(rest.parts) != 1:
                continue
            if not path.exists():
                names.add(rest.name)
        return sorted(names)
