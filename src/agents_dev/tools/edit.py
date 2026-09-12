"""写操作与 diff 预览。

写操作默认**不落盘**，只产生 diff。理由很直接：小模型出错率高，如果错误
直接污染真实代码，用户会丧失信任并最终弃用；而「先看 diff 再应用」几乎
零成本，既能在出错时一眼识破，又不打断思路。

所以全部写操作先进入 PendingChanges，由用户确认后才真正落盘。
"""

import difflib
import json
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec
from agents_dev.check.constraints import check_source, format_violations

# 项目里所有代码文件都不超过这个行数时，只提供整份重写。
#
# 实测把行号手术交给 7B 的代价：8 条失败里有 5 条死在 replace_lines 上——
# 它算错区间、拼错片段，留下重复行和悬空语句，而且看不出来自己错了。
# 同一批题里它的强项是「把整份文件交出来」：单发对照 25/25 用的就是那个形态。
# 选项少一个，它反而更容易做对。
WHOLE_FILE_LINE_LIMIT = 150

_SKIP_DIRS = frozenset({".agent", ".git", ".venv", "__pycache__", "node_modules"})

_TEST_DIRS = frozenset({"test", "tests"})


def is_test_path(path: str) -> bool:
    """这个路径看起来是不是测试文件。

    放在这里而不是验证模块：写工具和验证器都要用它，而反过来引会形成
    循环导入。
    """
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return False
    if pure.name.startswith("test_") or pure.name.endswith("_test.py"):
        return True
    return any(part.lower() in _TEST_DIRS for part in pure.parts[:-1])


def looks_like_path(text: str) -> bool:
    """这个字符串看起来是不是路径，而不是符号名。"""
    return "/" in text or "\\" in text or text.endswith(
        (".py", ".md", ".json", ".toml", ".txt")
    )


def is_small_project(root: Path, limit: int = WHOLE_FILE_LINE_LIMIT) -> bool:
    """项目里是否都是小文件。

    只要有一个文件超过阈值，就说明这个项目会需要行号级编辑，
    此时两把工具都要给。
    """
    for path in root.rglob("*.py"):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.count("\n") + 1 > limit:
            return False
    return True


def register_edit_tools(registry: Any, root: Path, pending: PendingChanges) -> None:
    """按项目规模决定给哪几把编辑工具。

    集中在一处：主循环和子智能体各注册一遍的话，两边的判定迟早会分叉，
    而那种分叉只会表现为「同一个项目里子智能体做得到、主循环做不到」。

    **注意 replace_text 不在这里注册**，这是量出来的结论，不是漏了。
    实测：给 6 道本来全过的题加上它之后，6 道全挂（0/6，连 token 数都一致），
    而且两次跑完全复现。它确实会被调用——问题出在拼出来的片段本身是坏的
    （比如 `try:` 下面少一个 `except`、缩进也不对）。这个模型擅长的是
    「交出一份完整文件」（单发 50/50 就是那个形态），不擅长拼片段。
    工具实现留着（有单元测试），换模型时可以再量一次。
    """
    registry.register(write_file_spec(root, pending))
    if not is_small_project(root):
        registry.register(replace_lines_spec(root, pending))


def replace_text_spec(root: Path, pending: PendingChanges) -> ToolSpec:
    """按唯一片段替换。改一处最省事的做法。

    另两把工具各有死穴，实测都撞过：replace_lines 要算行号，模型会算错区间、
    留下重复行和悬空语句（5 条失败死在它手上）；write_file 要复现整份，
    风险是丢掉原有内容，而且内容要经过 JSON 转义，出错面更大。

    片段替换两头都躲开了：只给出被替换的那一小段和新内容，既不需要行号，
    也不需要重写整个文件。代价是要求那段在文件里唯一——不唯一就报错，
    让它多带几行上下文，比猜一处改错强。
    """

    def transform(old_text: str, args: dict) -> str:
        old = args["old"]
        new = args["new"]
        if not old:
            raise ValueError("old 不能为空；要新增内容请用 write_file")
        if old not in old_text:
            raise ValueError(
                "文件里找不到这段内容。先 read_file 确认原文——"
                "缩进、空格、引号都要和文件里完全一致"
            )
        count = old_text.count(old)
        if count > 1:
            raise ValueError(
                f"这段内容在文件里出现了 {count} 次，无法确定改哪一处。"
                "请多带几行上下文（比如把上一行也包含进来），"
                "让它在文件里只出现一次"
            )
        return old_text.replace(old, new, 1)

    return ToolSpec(
        name="replace_text",
        description=(
            "把文件里某段唯一的内容替换成新内容；只生成 diff，需用户确认后才写入。"
            "改一处就用它：不必算行号，也不必把整个文件重写一遍"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要改的文件"},
                "old": {
                    "type": "string",
                    "description": "被替换的原内容，必须在文件里唯一出现",
                },
                "new": {
                    "type": "string",
                    "description": "替换成什么；要删掉这段就传空字符串",
                },
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
        handler=lambda args: _propose(
            root, pending, args, lambda old_text: transform(old_text, args)
        ),
    )


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

    if target.exists() and new_text == old_text:
        # 内容一字未变。照样登记的话，用户会被要求确认一处「什么都没改」的
        # 改动——diff 是空的，除了一点点养成「不看就点应用」的习惯，
        # 什么也换不来。同时也要让模型知道：这一写没产生改动，
        # 别以为自己已经改好了。
        return ToolResult(
            ok=True, content=f"{relative} 的内容与现有完全一致，没有产生改动。"
        )

    change = pending.propose(relative, new_text)
    verb = "新建" if change.is_new_file else "修改"
    body = f"已生成{verb}预览（尚未写入，需用户确认）：\n{change.diff}"

    # 测试文件是验收标准。改它不会让验证通过（验证按原始内容判定），
    # 但模型不知道这件事——实测它会把测试改成 assert True 然后宣布完成，
    # 白烧四步。与其事后拦，不如在它动手时说清楚。
    if is_test_path(relative):
        body = (
            "注意：这是测试文件。验证按测试的原始内容判定，"
            "改它不会让验证通过——测试失败说明代码不对。\n\n" + body
        )

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
        description=(
            "新建或整体覆盖一个文件，给出完整内容；只生成 diff，需用户确认后才写入。"
            "文件不大时首选它——整份给出比算行号区间更不容易出错"
        ),
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
        description=(
            "替换文件的指定行区间；只生成 diff，需用户确认后才写入。"
            "只用于大文件或只动一两行的情况"
        ),
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
