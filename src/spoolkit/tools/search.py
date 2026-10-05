"""代码搜索工具，基于 ripgrep。

未安装 rg 时返回失败结果并给出明确提示，不静默降级。

**读不了的路径要跳过并如实报告，不许把整条搜索判失败**（实测过的缺陷）：
本机沙箱里 `mkdir(mode=0o700)` 建出来的目录（pytest 缓存、`pytest-cache-files-*`）
自己就失去了访问权，`rg` 走到那里会打印一行
`rg: …\\pytest-cache-files-xxx: 拒绝访问。 (os error 5)` 并以 **2** 退出。
原先的实现把「退出码不是 0/1」一律当成搜索失败，于是**能读的部分搜到了也全被丢掉**，
模型只好换路重试（实测多走 3 步、督导介入从 2 涨到 7）。现在：跳过读不了的路径、
把「跳过了什么」写在结果里（不许静默），只有**别的**错误（比如正则写错）才算失败。
"""

import os
import shutil
import re
import subprocess
from pathlib import Path

from spoolkit.errors import PathOutsideProjectError
from spoolkit.paths import resolve_readable
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.tools.view import WorkspaceView

DEFAULT_MAX_RESULTS = 50
TIMEOUT_SECONDS = 20

#: rg 的退出码：0=有命中，1=没有命中，2=**有错误**。
#: 2 里最常见的一类是「某个路径读不了」。它不等于整条搜索失败——判据见 `_split_errors`。
RG_ERROR_EXIT = 2

#: 「这个路径读不了」在 stderr 里的形状（中英文系统话术都算）。
#: 不认得的错误一律按「别的错误」处理（宁可报失败，也不许静默吞掉）。
_UNREADABLE_HINTS = (
    "os error 5",  # ERROR_ACCESS_DENIED
    "os error 13",  # EACCES
    "permission denied",
    "access is denied",
    "拒绝访问",
)
#: `rg: <路径>: <系统错误>`。非贪婪取路径 + **要求冒号后面真的有空白**：
#: Windows 盘符里的冒号后面跟的是 `\`，不能在那里把路径切断
#: （写成 `:\s*` 会把 `D:\workSpace\...\pytest-bt1` 截成 `D`）。
_ERROR_LINE = re.compile(r"^rg:\s*(?P<path>.+?):\s+(?P<why>\S.*)$")
#: 结果里最多点名几个被跳过的路径（点太多会把结果本身挤没）；剩下的只报个数。
_SKIP_SHOWN = 5


def _skip_note(skipped, limit=_SKIP_SHOWN) -> str:
    """「跳过了什么」必须看得见：静默跳过等于假装搜全了。"""
    if not skipped:
        return ""
    shown = [path for path, _ in skipped[:limit]]
    more = len(skipped) - len(shown)
    tail = f"，还有 {more} 个" if more else ""
    return (
        f"（跳过 {len(skipped)} 个不可读路径：{'、'.join(shown)}{tail}"
        "——这些地方没搜到，不是「它们里面没有匹配」）"
    )


def _split_errors(stderr: str):
    """把 rg 的 stderr 分成「读不了的路径」与「别的错误」。

    返回 `(unreadable, others)`：`unreadable` 是 `[(路径, 原因), …]`，
    `others` 是认不出是权限/IO 的那些行——**它们仍然算失败**，
    否则「搜索失败」这件事会被静默吞掉（那正是这次要修的另一半）。
    """
    unreadable: list[tuple[str, str]] = []
    others: list[str] = []
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ERROR_LINE.match(line)
        path = match.group("path").strip() if match else ""
        why = (match.group("why") if match else line).strip()
        if path and any(hint in why.lower() for hint in _UNREADABLE_HINTS):
            unreadable.append((path, why))
        else:
            others.append(line)
    return unreadable, others


def _same_path(left: str, right: Path) -> bool:
    """两个路径是不是同一处（大小写与分隔符都不敏感）。"""
    def norm(text: str) -> str:
        return os.path.normcase(os.path.normpath(str(text))).rstrip("\\/")

    return norm(left) == norm(right)


def _search_code(root: Path, args: dict, pending=None, read_roots=()) -> ToolResult:
    pattern = args["pattern"]
    max_results = args.get("max_results", DEFAULT_MAX_RESULTS)
    if max_results < 1:
        return ToolResult(ok=False, content="max_results 必须大于等于 1")

    if shutil.which("rg") is None:
        return ToolResult(ok=False, content="未找到 rg（ripgrep），无法执行搜索")

    try:
        base = resolve_readable(root, args.get("path", "."), read_roots)
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    proc = subprocess.run(
        [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(max_results),
            "--",
            pattern,
            str(base),
        ],
        capture_output=True,
        text=True,
        # 必须显式指定 UTF-8。默认走 Windows 本地编码（GBK），
        # 源码里一旦出现超出 GBK 的字符，解码失败会让 stdout 变成 None，
        # 报错信息却是 "NoneType has no attribute splitlines"——离真正原因很远。
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        cwd=str(root),
    )

    unreadable, other_errors = _split_errors(proc.stderr)
    note = _skip_note(unreadable)
    # 退出码不是 rg 认识的三个之一、stderr 里又什么都没说：不许当成功
    # （「不许静默」这条对失败也成立）。
    if proc.returncode not in (0, 1, RG_ERROR_EXIT) and not other_errors:
        return ToolResult(
            ok=False,
            content=f"搜索失败: rg 退出码 {proc.returncode}（stderr 没有说明原因）",
        )
    # 「别的错误」（正则写错、参数写错…）：**仍然报失败**，不静默吞掉；
    # 但跳过的路径也一并说出来，否则人以为只是正则的问题。
    if other_errors:
        return ToolResult(
            ok=False,
            content=f"搜索失败: {' '.join(other_errors)}" + (f"\n{note}" if note else ""),
        )

    root_prefix = str(root)
    lines = proc.stdout.splitlines()[:max_results]
    cleaned = [
        line.replace(root_prefix + "\\", "").replace(root_prefix + "/", "")
        for line in lines
    ]

    # 待确认的改动不在磁盘上，rg 搜到的是旧内容。改过的文件整份改用
    # 「改之后」的内容重搜，否则模型会搜到自己刚删掉的东西。
    view = WorkspaceView(root, pending)
    if view.overridden:
        stale = set(view.overridden)
        merged = [line for line in cleaned if line.split(":", 1)[0] not in stale]
        merged.extend(_search_overlay(view, pattern, base, max_results))
        cleaned = merged[:max_results]

    if cleaned:
        content = "\n".join(cleaned)
        return ToolResult(ok=True, content=content + (f"\n{note}" if note else ""))

    # 一条都没搜到。这时「没匹配」与「根本没搜成」必须分得开：搜索的**起点**
    # 自己就读不了的话，这次搜索一次都没真正执行过，不能拿空结果冒充成功。
    if unreadable and any(_same_path(path, base) for path, _ in unreadable):
        return ToolResult(
            ok=False,
            content=(
                f"没有搜成：{args.get('path', '.')} 本身读不了"
                f"（{[why for path, why in unreadable if _same_path(path, base)][0]}），"
                "所以这次搜索**一条都没搜**——这不是「没有匹配」。"
                "换一个可读的目录，或者先把它的权限修好。\n"
                + _skip_note(unreadable)
            ),
        )
    return ToolResult(ok=True, content=note)


def _search_overlay(
    view: WorkspaceView, pattern: str, base: Path, max_results: int
) -> list[str]:
    """在待确认改动的内容里搜。只处理改动涉及的那几个文件。"""
    try:
        expression = re.compile(pattern)
    except re.error:
        return []  # 正则本身不合法时，rg 已经报过错了

    try:
        base_rel = view.relative(base)
    except ValueError:
        base_rel = "."

    def in_scope(path: str) -> bool:
        if base_rel in (".", ""):
            return True  # 全项目搜索
        if base.is_file():
            return path == base_rel
        return path.startswith(base_rel + "/")

    hits: list[str] = []
    for path in view.overridden:
        if not in_scope(path):
            continue
        text = view.overridden_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                hits.append(f"{path}:{number}:{line}")
                if len(hits) >= max_results:
                    return hits
    return hits


def search_code_spec(root: Path, pending=None, read_roots=()) -> ToolSpec:
    """构造代码搜索工具的规格。"""
    return ToolSpec(
        name="search_code",
        description="在项目内按正则搜索代码，返回 文件:行号:内容",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "在哪个目录或文件里搜"},
                "max_results": {"type": "integer", "description": "最多返回几条"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=lambda args: _search_code(root, args, pending, read_roots),
        brief="正则搜代码",
        group="看",
    )

