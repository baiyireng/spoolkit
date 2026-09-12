"""目录统计工具。

Agent 原先只能「一个一个看」（list_dir、search_code），没有任何"量一下"的
能力。于是问到「哪个目录最大」时，它只能顺口编一个——实测它就这么干了。

这不是「换个大模型就好了」的问题：**没有测量工具时，任何模型都只能猜。**
所以这里补的是一个通用能力（任何已授权的路径都能调），不是一条固定流程——
量什么、怎么归纳、要不要再深挖，仍然由模型自己决定。

代价必须有界：40 万文件的目录树，光走一遍就是几秒；所以带时间预算与条目
上限，到量就停并**明说这次统计不完整**——不完整的统计被当成完整的用，
比没有统计更糟。
"""

import os
import time
from collections import Counter
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_readable
from agents_dev.tools.types import Fact, ToolResult, ToolSpec

DEFAULT_SECONDS = 12.0
# 条目上限只作安全网（防病态目录树），不该比时间预算先触发——
# 实测 40 万文件的树撞的就是它，于是「扫了多少」由条数决定而不是由时间决定。
MAX_ENTRIES = 2_000_000
TOP = 8


def _size_text(value: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{value} B"


def _collect(root: Path, seconds: float) -> dict:
    """走一遍。返回聚合结果；被预算截断时 truncated 为真。"""
    started = time.time()
    total_files = 0
    total_bytes = 0
    by_suffix: Counter = Counter()
    suffix_bytes: Counter = Counter()
    by_child: dict[str, list[int]] = {}
    largest: list[tuple[int, str]] = []
    oldest: list[tuple[float, str]] = []
    truncated = False

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        # os.scandir 的目录项自带类型缓存，比 iterdir + 多次 stat 快得多。
        with iterator:
            for entry in iterator:
                if total_files >= MAX_ENTRIES or time.time() - started > seconds:
                    truncated = True
                    break
                try:
                    # 不跟进符号链接：目录环会把一次统计变成死循环
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat()
                except (OSError, PermissionError):
                    continue

                total_files += 1
                total_bytes += stat.st_size
                suffix = Path(entry.name).suffix.lower() or "（无扩展名）"
                by_suffix[suffix] += 1
                suffix_bytes[suffix] += stat.st_size

                path = Path(entry.path)
                try:
                    top = path.relative_to(root).parts[0]
                except ValueError:
                    top = "(本身)"
                bucket = by_child.setdefault(top, [0, 0])
                bucket[0] += 1
                bucket[1] += stat.st_size

                largest.append((stat.st_size, str(path)))
                largest.sort(reverse=True)
                del largest[TOP:]
                oldest.append((stat.st_mtime, str(path)))
                oldest.sort()
                del oldest[TOP:]
        if truncated:
            break

    return {
        "files": total_files,
        "bytes": total_bytes,
        "by_suffix": by_suffix,
        "suffix_bytes": suffix_bytes,
        "by_child": by_child,
        "largest": largest,
        "oldest": oldest,
        "truncated": truncated,
    }


def _render(root: Path, data: dict, top: int) -> str:
    lines = [f"{root}", f"合计：{data['files']} 个文件，{_size_text(data['bytes'])}"]
    if data["truncated"]:
        lines.append("⚠ 这次统计**不完整**（撞到时间或条目上限），下面的排序只是已扫到的部分。")

    children = sorted(
        data["by_child"].items(), key=lambda item: item[1][1], reverse=True
    )[:top]
    if children:
        lines.append("")
        lines.append("按直接子项（大小降序）：")
        lines.extend(
            f"  {_size_text(size):>10}  {count:>8} 个文件  {name}"
            for name, (count, size) in children
        )

    suffixes = data["by_suffix"].most_common(top)
    if suffixes:
        lines.append("")
        lines.append("按类型（数量降序）：")
        lines.extend(
            f"  {count:>8} 个  {_size_text(data['suffix_bytes'][suffix]):>10}  {suffix}"
            for suffix, count in suffixes
        )

    if data["largest"]:
        lines.append("")
        lines.append("最大的文件：")
        lines.extend(
            f"  {_size_text(size):>10}  {path}" for size, path in data["largest"][:top]
        )

    if data["oldest"]:
        lines.append("")
        lines.append("最久没改过的文件：")
        lines.extend(
            f"  {time.strftime('%Y-%m-%d', time.localtime(mtime))}  {path}"
            for mtime, path in data["oldest"][:top]
        )
    return "\n".join(lines)


def _facts(data: dict, top: int) -> tuple[Fact, ...]:
    """把「谁是多少」结构性地交出来，供数字核对判断有没有配错对象。

    渲染出来的文本是给模型读的；这一份是给核对器用的。
    """
    found: list[Fact] = [
        Fact("合计", float(data["files"]), "个文件"),
        Fact("合计", float(data["bytes"]), "B"),
    ]
    for name, (count, size) in data["by_child"].items():
        found.append(Fact(name, float(size), "B"))
        found.append(Fact(name, float(count), "个文件"))
    for suffix, count in data["by_suffix"].items():
        found.append(Fact(suffix, float(data["suffix_bytes"][suffix]), "B"))
        found.append(Fact(suffix, float(count), "个"))
    for size, path in data["largest"]:
        found.append(Fact(Path(path).name, float(size), "B"))
    return tuple(found)


def _dir_stats(root: Path, args: dict, read_roots=()) -> ToolResult:
    try:
        base = resolve_readable(root, args["path"], read_roots)
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))
    if not base.is_dir():
        return ToolResult(ok=False, content=f"不是目录: {args['path']}")

    seconds = float(args.get("seconds") or DEFAULT_SECONDS)
    if not 1 <= seconds <= 120:
        return ToolResult(ok=False, content="seconds 必须在 1 到 120 之间")
    top = int(args.get("top") or TOP)
    if not 1 <= top <= 30:
        return ToolResult(ok=False, content="top 必须在 1 到 30 之间")

    data = _collect(base, seconds)
    return ToolResult(
        ok=True,
        content=_render(base, data, top),
        facts=_facts(data, top),
    )


def dir_stats_spec(root: Path, read_roots=()) -> ToolSpec:
    """对一个目录做规模统计。"""
    return ToolSpec(
        name="dir_stats",
        description=(
            "统计一个目录的规模：总量、按直接子项和文件类型的大小分布、"
            "最大的文件、最久没改的文件。要判断「哪里占地方」「哪些像是能清的」"
            "时用它——先量再判断，不要凭印象说"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要统计的目录"},
                "top": {"type": "integer", "description": "每节列几条，默认 8"},
                "seconds": {
                    "type": "integer",
                    "description": "时间预算，默认 12 秒；目录很大时会扫不全",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _dir_stats(root, args, read_roots),
        brief="量目录规模",
        group="量",
    )
