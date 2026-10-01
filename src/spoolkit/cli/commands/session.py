"""会话管理命令。

建了会话却管不了它，等于没有会话概念——你甚至不知道自己建过哪些。

这一版把会话做成**有生命周期**的：`list` / `new` / `use` / `rename` / `archive`。
列出来的那个序号就是 `use <序号>` 认的序号——两处都按同一份完整列表（含已归档）
编号，所以照着自己看到的数字敲回去一定落在同一条上。归档的条目在默认列表里
不显示，但仍然可以直接用 id 进去（设计文档 §3.3 / §4.1）。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from spoolkit import limits
from spoolkit.kernel import pointers, sessions
from spoolkit.memory.transcript import (
    no_hits_text,
    parse_moment,
    render_history_hits,
    resolve_scope,
    search_messages,
)
from spoolkit.store.db import open_db

CLIENT = "cli"


def _stamp(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M")


def _current_session(root: Path) -> str:
    """指针里那条**属于这个工作区**的会话（会话不能跨工作区搬）。"""
    pointer = pointers.get(CLIENT)
    if pointer is None or pointer.workspace != root:
        return ""
    return pointer.session


def session_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    action = str(getattr(args, "action", "list") or "list")
    name = str(getattr(args, "name", "") or "").strip()
    title = str(getattr(args, "title", "") or "").strip()

    if action == "search":
        return _search(root, name, args)

    if action == "new":
        # `spool session new <标题>`：`new` 后面那一个位置参数**就是标题**（设计
        # §5.1 的 `new [标题]`）。argparse 的槽位名叫 `name`，于是这里取
        # `title or name`——两种写法都认，免得"建出来的会话没有标题"。
        made = sessions.create_session(root, title=title or name)
        pointers.set(CLIENT, root, made)
        heading = title or name
        print(f"新会话：{made}" + (f"（{heading}）" if heading else ""))
        return 0
    if action == "use":
        try:
            picked = sessions.resolve_session(root, name)
        except sessions.SessionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        pointers.set(CLIENT, root, picked)
        print(f"现在用会话：{picked}")
        return 0
    if action == "rename":
        if not title:
            # 不给新标题就**报错，绝不静默清空**：标题是这个会话唯一的"人认得的名字"，
            # 而它没有历史可回滚（全分支评审判它是唯一"照常使用就会损坏数据"的一条）。
            print(
                "改名要给新标题：`spool session rename <id|序号> 新标题`。",
                file=sys.stderr,
            )
            return 2
        try:
            sessions.rename_session(root, name, title)
        except sessions.SessionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"已改名：{name} → {title}")
        return 0
    if action == "archive":
        try:
            sessions.archive_session(root, name)
        except sessions.SessionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("已归档（数据仍保留，默认不再列出；`spool session --all` 可见）")
        return 0
    return _list(root, include_archived=bool(getattr(args, "all", False)))


def _default_session(root: Path) -> str:
    """这条命令的「当前会话」：指针里那条；没有指针就用最近活跃的那条。

    指针为空（没切过会话）时退回 `latest`，与 `spool session use latest` 同一个
    含义——默认作用域必须在**每条**入口上都指得出一个具体的会话，否则
    「默认当前会话」会退化成「默认全部会话」，那正是这次要改掉的东西。
    """
    here = _current_session(root)
    if here:
        return here
    listed = sessions.list_sessions(root, include_archived=True)
    return listed[0].id if listed else ""


def _search(root: Path, query: str, args: argparse.Namespace) -> int:
    """`spool session search <关键词>`：检索历史消息，给片段 + 出处。

    和模型那个 `search_history` 走**同一个** `search_messages` 与同一个
    `resolve_scope`：两条路的口径（排序、片段长度、脱敏、时间范围、作用域）
    只有一份，不各写一套。

    **默认只查当前会话**（指针里那条，没有就用最近活跃的那条）；跨会话要
    显式 `--all-sessions`（等价 `session="*"`），也可以用 `--session` 点名
    某一条。工作区边界不动：跨工作区依旧不查。
    """
    if not query:
        print("需要关键词：`spool session search <关键词>`。", file=sys.stderr)
        return 2

    path = root / ".agent" / "memory.db"
    if not path.is_file():
        print("这个工作区还没有任何会话。", file=sys.stderr)
        return 2

    overrides = limits.load_overrides(root)
    wanted = int(getattr(args, "limit", 0) or 0)
    max_limit = int(limits.resolve("history_search_max", overrides)[0])
    # `0` = 没给（argparse 的默认值），按标定值；给了就越界判定，别静默改成别的数。
    if wanted and not 1 <= wanted <= max_limit:
        print(f"--limit 必须在 1 到 {max_limit} 之间（不给 = 按标定值）。", file=sys.stderr)
        return 2
    limit = wanted or int(limits.resolve("history_search_limit", overrides)[0])
    snippet_chars = int(limits.resolve("history_snippet_chars", overrides)[0])

    # 作用域：`--all-sessions` 是显式的「全部」；`--from-session` 点名一条；
    # 两个都没给才是当前会话（`resolve_scope` 认的空串/`*` 与它同一份规则）。
    # 用 `--from-session` 而不是 `--session`：后者在各入口里是**运行用的会话**
    # （内核解析、写指针），`session` 子命令借它当检索作用域会让两件事同名。
    named = str(getattr(args, "from_session", "") or "").strip()
    if getattr(args, "all_sessions", False):
        named = "*"
    current = _default_session(root)
    if not named and not current:
        print(
            "这个工作区还没有会话，说不出「当前会话」是哪个："
            "用 `--all-sessions` 查全部，或 `--from-session <会话>` 点名一条。",
            file=sys.stderr,
        )
        return 2
    scope = resolve_scope(named, current)
    cross_session = not scope.session

    try:
        since = (
            parse_moment(args.since, label="--since")
            if str(getattr(args, "since", "") or "").strip()
            else None
        )
        until = (
            parse_moment(args.until, label="--until")
            if str(getattr(args, "until", "") or "").strip()
            else None
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    conn = open_db(path)
    try:
        hits = search_messages(
            conn,
            query,
            limit=limit,
            snippet_chars=snippet_chars,
            session=scope.session,
            since=since,
            until=until,
        )
    finally:
        conn.close()

    if not hits:
        print(
            no_hits_text(
                query, scope_said=scope.description, cross_session=cross_session
            )
        )
        # 没命中给 1：脚本里"搜到没搜到"要能直接判，别混进"用法错"(2)。
        return 1
    titles = {
        item.id: item.title for item in sessions.list_sessions(root, include_archived=True)
    }
    print(
        render_history_hits(
            hits,
            titles,
            cross_session=cross_session,
            scope_said=scope.description,
        )
    )
    return 0


def _list(root: Path, include_archived: bool = False) -> int:
    # 编号取**完整**列表（含已归档），与 `resolve_session` 是同一个空间：
    # 默认只打印没归档的那几行，于是序号会出现空档——那是在如实说明
    # "中间还有几条被藏起来了"，比"看得见的第 3 条却是另一条"要好。
    everything = sessions.list_sessions(root, include_archived=True)
    if not everything:
        if not (root / ".agent" / "memory.db").exists():
            print("这个工作区还没有任何会话。", file=sys.stderr)
            return 2
        print("还没有会话记录。", file=sys.stderr)
        return 2

    here = _current_session(root)
    print(f"{'会话':<18}{'标题':<24}{'模型':<20}{'窗口':>7}{'最近活跃':>14}")
    shown = 0
    for index, item in enumerate(everything, start=1):
        if item.archived and not include_archived:
            continue
        shown += 1
        marks = []
        if item.id == here:
            marks.append("当前")
        if item.archived:
            marks.append("已归档")
        tail = f"  ← {'、'.join(marks)}" if marks else ""
        print(
            f"{index:>3}) {item.id:<14}{(item.title or '—'):<24}"
            f"{(item.model or '—'):<20}{item.context_limit:>7}"
            f"{_stamp(item.last_active):>14}{tail}"
        )

    print()
    if shown == 0:
        print(f"共 {len(everything)} 个会话，均已归档。`spool session --all` 可见。")
    elif include_archived:
        print(f"共 {len(everything)} 个会话（含已归档）。用 `spool session use <序号|id>` 切。")
    else:
        print(f"共 {shown} 个会话。用 `spool session use <序号|id|latest>` 切。")
    return 0
