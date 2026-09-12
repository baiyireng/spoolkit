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

    # --- 查询 ---

    def relative(self, target: Path) -> str:
        """绝对路径 → 项目内相对路径（POSIX 形式）。

        工作区之外的路径（额外可读根）返回空串：那种路径本来就不可能有
        待确认的改动，调用方按「没被改过」处理即可——而不是抛异常。
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

    def exists(self, target: Path) -> bool:
        return self.relative(target) in self._overlay() or target.exists()

    def is_dir(self, target: Path) -> bool:
        # 待确认的改动只可能是文件，不会凭空造出目录。
        return target.is_dir()

    def read_text(self, target: Path) -> str:
        """读文件内容。改过的文件给改之后的内容。"""
        relative = self.relative(target)
        overlay = self._overlay()
        if relative in overlay:
            return overlay[relative]
        return target.read_text(encoding="utf-8")

    def overlay_children(self, directory: Path) -> list[str]:
        """这个目录下有哪些文件只存在于待确认改动里（磁盘上还没有）。"""
        relative = self.relative(directory)
        if relative == "":
            return []  # 工作区之外的目录，谈不上有本项目的待确认改动
        prefix = "" if relative == "." else relative + "/"
        names = set()
        for path in self._overlay():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            # 更深一层的不算直接子项；磁盘上已有的也不用补。
            if not rest or "/" in rest:
                continue
            if not (directory / rest).exists():
                names.add(rest)
        return sorted(names)
