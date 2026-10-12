"""文件系统工具。

所有路径都经 resolve_within 处理，模型无法逃出项目根目录。
工具失败一律返回 ok=False，不抛异常。
"""

from pathlib import Path

from spoolkit.errors import PathOutsideProjectError
from spoolkit.paths import resolve_readable
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.tools.view import WorkspaceView


def _read_file(root: Path, args: dict, pending=None, read_roots=()) -> ToolResult:
    try:
        target = resolve_readable(root, args["path"], read_roots)
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    # 待确认的改动优先：模型刚写完的文件必须读到它自己写的内容，
    # 否则它会以为写入没生效（而 run_command 那边看到的是新内容）。
    view = WorkspaceView(root, pending)

    if not view.exists(target):
        # 删除也要出一句人话：只说「文件不存在」时，模型分不清是它自己刚
        # 删掉的（那是预期状态）、还是路径写错了（那是要改的）。
        if target.resolve() in view.deleted_paths():
            # fix-bug 真机现场：模型误删文件后想恢复，反复 read 同一个路径
            # 9 次——它以为「读不到 = 出问题了，再读一次」。必须把出路写死：
            # 删除还没落盘，磁盘上的旧内容还在；要恢复就重新 write_file
            # 写回内容（旧内容可以先用 run_command 跑 `type`/`cat` 读磁盘拿到，
            # 因为 read_file 看的是「改动生效后」的视图，而 run_command 看的
            # 是真实磁盘）。
            return ToolResult(
                ok=False,
                content=(
                    f"{args['path']} 已在待确认改动里被删除（还没落盘）——"
                    "现在读不到它，这是删除生效后的正常状态，**再读多少次都一样**。\n"
                    "如果删除是误操作、想恢复它：磁盘上的旧文件还在，"
                    "用 run_command 跑 `type <路径>`（Windows）或 `cat <路径>`（POSIX）"
                    "拿到旧内容，再用 write_file 写回去——新写入会顶替掉那处待确认的删除。"
                ),
            )
        return ToolResult(ok=False, content=f"文件不存在: {args['path']}")
    if view.is_dir(target):
        return ToolResult(ok=False, content=f"目标是目录而非文件: {args['path']}")

    try:
        text = view.read_text(target)
    except FileNotFoundError:
        return ToolResult(ok=False, content=f"文件不存在: {args['path']}")
    except UnicodeDecodeError:
        return ToolResult(ok=False, content=f"文件不是 UTF-8 文本: {args['path']}")

    start = args.get("start_line")
    end = args.get("end_line")
    if start is None and end is None:
        return ToolResult(ok=True, content=text)

    start = 1 if start is None else start
    if start < 1:
        return ToolResult(ok=False, content="start_line 必须大于等于 1")

    lines = text.splitlines()
    end = len(lines) if end is None else end
    if end < start:
        return ToolResult(ok=False, content="end_line 不能小于 start_line")

    chunk = lines[start - 1 : end]
    numbered = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))
    return ToolResult(ok=True, content=numbered)


def _list_dir(root: Path, args: dict, pending=None, read_roots=()) -> ToolResult:
    try:
        target = resolve_readable(root, args["path"], read_roots)
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    if not target.exists():
        return ToolResult(ok=False, content=f"目录不存在: {args['path']}")
    if not target.is_dir():
        return ToolResult(ok=False, content=f"目标不是目录: {args['path']}")

    # 读不了的目录要给人话，不许抛出去（本机沙箱里 0o700 建出来的目录就是这样：
    # 目录本身存在、`is_dir()` 也为真，但一列举就 PermissionError）。
    # 「空目录」与「读不了」是两件事，混成一件模型会以为这里真的什么都没有。
    try:
        children = sorted(target.iterdir())
    except OSError as exc:
        return ToolResult(
            ok=False,
            content=(
                f"目录读不了（{exc.strerror or exc}）: {args['path']}"
                "——这是权限/IO 问题，**不是「空目录」**；"
                "换一个可读的目录，或者先把它的权限修好。"
            ),
        )

    entries: list[str] = []
    unreadable: list[str] = []
    for child in children:
        try:
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
        except OSError:
            # 读不了的**单个条目**也要留痕：静默跳过等于告诉模型「这里没有它」。
            unreadable.append(child.name)
    # 待确认改动里新建的文件在磁盘上还不存在，但模型应该看得到它们；
    # 反过来，待确认删除的文件不该再被列出来——列出来它就会以为没删掉。
    view = WorkspaceView(root, pending)
    gone = {
        path.name
        for path in view.deleted_paths()
        if path.parent.resolve() == target.resolve()
    }
    if gone:
        entries = [name for name in entries if name not in gone]
    known = {name.rstrip("/") for name in entries}
    for name in view.overlay_children(target):
        if name not in known:
            entries.append(name)
    if unreadable:
        entries.append(_unreadable_note(unreadable))
    return ToolResult(ok=True, content="\n".join(entries))


def _unreadable_note(unreadable: list[str]) -> str:
    """跳过的条目要可见（改名/改名残留类判断全靠这份列表可信）。"""
    shown = unreadable[:5]
    more = len(unreadable) - len(shown)
    return (
        f"（跳过 {len(unreadable)} 个读不了的条目：{'、'.join(shown)}"
        + (f"，还有 {more} 个" if more else "")
        + "）"
    )


def read_file_spec(root: Path, pending=None, read_roots=()) -> ToolSpec:
    """构造读文件工具的规格。"""
    return ToolSpec(
        name="read_file",
        description="读取项目内文件；可用 start_line/end_line 只取需要的行区间",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _read_file(root, args, pending, read_roots),
        brief="读文件（可只取一段）",
        group="看",
    )


def list_dir_spec(root: Path, pending=None, read_roots=()) -> ToolSpec:
    """构造列目录工具的规格。"""
    return ToolSpec(
        name="list_dir",
        description="列出项目内某个目录的直接子项",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _list_dir(root, args, pending, read_roots),
        brief="列目录",
        group="看",
    )

