"""写操作与 diff 预览。

写操作默认**不落盘**，只产生 diff。理由很直接：小模型出错率高，如果错误
直接污染真实代码，用户会丧失信任并最终弃用；而「先看 diff 再应用」几乎
零成本，既能在出错时一眼识破，又不打断思路。

所以全部写操作先进入 PendingChanges，由用户确认后才真正落盘。
"""

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec
from agents_dev.check.constraints import check_source, format_violations


def make_diff(path: str, old: str, new: str) -> str:
    """生成统一格式的差异文本。"""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


@dataclass
class PendingChange:
    """一处待授权的修改。"""

    path: str
    old_text: str
    new_text: str

    @property
    def is_new_file(self) -> bool:
        return not self.old_text

    @property
    def diff(self) -> str:
        if self.is_new_file:
            body = "".join(f"+{line}" for line in self.new_text.splitlines(True))
            return f"--- /dev/null\n+++ b/{self.path}\n{body}"
        return make_diff(self.path, self.old_text, self.new_text)


class PendingChanges:
    """本次运行中提出、尚未落盘的修改集合。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._changes: dict[str, PendingChange] = {}

    def __len__(self) -> int:
        return len(self._changes)

    def items(self) -> list[PendingChange]:
        return list(self._changes.values())

    def path_of(self, candidate: str) -> str:
        """把用户给的路径解析成项目内相对路径。"""
        target = resolve_within(self._root, candidate)
        return target.relative_to(self._root).as_posix()

    def propose(self, path: str, new_text: str) -> PendingChange:
        """登记一处修改。同一路径重复提出时以最后一次为准。"""
        target = self._root / path
        old_text = target.read_text(encoding="utf-8") if target.exists() else ""
        change = PendingChange(path=path, old_text=old_text, new_text=new_text)
        self._changes[path] = change
        return change

    def apply(self) -> list[str]:
        """把全部待授权修改写入磁盘，返回已写入的路径。

        落盘之前先构造基线。没有基线的写操作等于没有退路，
        而「确认错了」是必然会发生的——diff 看得再仔细也挡不住走神。
        """
        written: list[str] = []
        for change in self._changes.values():
            target = self._root / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_text, encoding="utf-8")
            written.append(change.path)
        self._changes.clear()
        return written

    def baseline(self) -> "Baseline":
        """把当前待写入的修改转成可持久化的基线。"""
        return Baseline(
            entries=tuple(
                BaselineEntry(
                    path=change.path,
                    old_text=None if change.is_new_file else change.old_text,
                )
                for change in self._changes.values()
            )
        )

    def discard(self) -> None:
        self._changes.clear()


@dataclass(frozen=True)
class BaselineEntry:
    """一个文件的改动前状态。old_text 为 None 表示这个文件是新建的。"""

    path: str
    old_text: str | None


@dataclass(frozen=True)
class Baseline:
    """一次写操作的改动前快照。"""

    entries: tuple[BaselineEntry, ...]

    def to_json(self) -> str:
        return json.dumps(
            {"entries": [{"path": e.path, "old_text": e.old_text} for e in self.entries]},
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Baseline":
        payload = json.loads(text)
        return cls(
            entries=tuple(
                BaselineEntry(path=item["path"], old_text=item.get("old_text"))
                for item in payload.get("entries", [])
            )
        )


def save_baseline(path: Path, baseline: Baseline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.to_json(), encoding="utf-8")


def load_baseline(path: Path) -> Baseline | None:
    if not path.exists():
        return None
    return Baseline.from_json(path.read_text(encoding="utf-8"))


def revert(root: Path, baseline: Baseline) -> list[str]:
    """把文件恢复到基线状态，返回被处理过的路径。

    新建的文件在回滚时删除——恢复到「它不存在」才算真的恢复。
    """
    touched: list[str] = []
    for entry in baseline.entries:
        target = root / entry.path
        if entry.old_text is None:
            if target.exists():
                target.unlink()
            touched.append(entry.path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.old_text, encoding="utf-8")
        touched.append(entry.path)
    return touched


def _propose(
    root: Path, pending: PendingChanges, args: dict, transform
) -> ToolResult:
    """共用的提交流程：解析路径、读取原文、变换、登记、返回 diff。

    变换失败用异常表达。曾经让变换函数「成功返回新内容、失败返回错误说明」，
    两者都是字符串，结果成功路径被误判成失败——用异常区分才不会有歧义。
    """
    try:
        relative = pending.path_of(args["path"])
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    target = root / relative
    old_text = target.read_text(encoding="utf-8") if target.exists() else ""
    try:
        new_text = transform(old_text)
    except ValueError as exc:
        return ToolResult(ok=False, content=str(exc))

    change = pending.propose(relative, new_text)
    verb = "新建" if change.is_new_file else "修改"
    body = f"已生成{verb}预览（尚未写入，需用户确认）：\n{change.diff}"

    # 写完立刻检查结构约束。这是「软要求变硬反馈」的落点：
    # 模型不需要记住规则，只需要对具体违反项作出反应。
    feedback = format_violations(check_source(new_text, relative))
    if feedback:
        body = f"{body}\n\n{feedback}"
    return ToolResult(ok=True, content=body)


def write_file_spec(root: Path, pending: PendingChanges) -> ToolSpec:
    """整文件写入。对已存在的文件是整体替换，改动量大时慎用。"""

    def handler(args: dict) -> ToolResult:
        return _propose(root, pending, args, lambda _old: args["content"])

    return ToolSpec(
        name="write_file",
        description="新建或整体覆盖一个文件；只生成 diff，需用户确认后才写入",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def replace_lines_spec(root: Path, pending: PendingChanges) -> ToolSpec:
    """按行区间替换。比整文件写入精确得多，是改动的首选方式。"""

    def transform(old_text: str, args: dict) -> str:
        if not old_text:
            raise ValueError("文件不存在，无法按行替换；请先确认路径")
        lines = old_text.splitlines()
        start = args["start_line"]
        end = args["end_line"]
        if start < 1 or end < start or end > len(lines):
            raise ValueError(f"行号区间非法：文件共 {len(lines)} 行")
        replacement = args["content"].splitlines()
        merged = lines[: start - 1] + replacement + lines[end:]
        return "\n".join(merged) + ("\n" if old_text.endswith("\n") else "")

    def handler(args: dict) -> ToolResult:
        return _propose(root, pending, args, lambda old: transform(old, args))

    return ToolSpec(
        name="replace_lines",
        description="替换文件的指定行区间；只生成 diff，需用户确认后才写入",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "content": {"type": "string"},
            },
            "required": ["path", "start_line", "end_line", "content"],
            "additionalProperties": False,
        },
        handler=handler,
    )
