"""会话：属于某个工作区的一条活。

为什么要有它：`.agent/sessions/<会话>/`（计划、进度、检查点）与 `memory.db` 的
会话表都已经在了，但会话只是在**启动时**用 `--session` 选一次，还被各入口硬编码了
不同的默认名。于是"一个工作区多个会话"只停留在文件结构上。

状态落在两处，各有理由：

- `memory.db` 的会话表：最近活跃、模型、窗口——真实数据，进库；
- `.agent/sessions/<id>/meta.json`：标题、归档标记——给人看的旁路信息，
  **不动 `memory.db` 的表结构**（那库里装着记忆，不值得为标题冒迁移的险）。

归档 = 从默认列表里隐藏：计划、进度、检查点、记忆都不动，也仍然能直接进去。
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spoolkit.kernel import jsonstore
from spoolkit.memory.store import init_memory_schema, list_sessions as db_sessions
from spoolkit.memory.store import record_session
from spoolkit.session_state import safe
from spoolkit.store.db import open_db

META_NAME = "meta.json"
TITLE_LIMIT = 40

#: 标题是谁起的。`seed` 是"先拿第一句目标顶上"，`agent` 是模型起的名，
#: `manual` 是人点名的（`spool session new/rename`）。只有 seed 与 agent 可以被
#: 更新；没有来源的老标题按 manual 处理——不许为了一个新功能把人的命名盖掉。
TITLE_SEED = "seed"
TITLE_AGENT = "agent"
TITLE_MANUAL = "manual"

#: 会话名（人点名的那个）的长度上限。
#:
#: 自动 id 是 `250101-120000-abcd` 这种 20 个字符，48 是它的两倍多：长到人还认得出、
#: 还能当目录名用，也不至于把左栏那一行撑爆。
NAME_LIMIT = 48

#: 会话名允许的形状：与 `safe()` 会**原样保留**的那一套一致（中英文、数字、
#: `_`、`-`、`.`）。为什么严格校验而不是"交给 `safe()` 悄悄改掉"：
#: `safe("a b")` 与 `safe("a_b")` 会落到**同一个目录**，两条活共用一个
#: `meta.json`——那是静默的数据损坏，不是"名字被规整了"。
_NAME_SHAPE = re.compile(r"^[\w.-]+$", re.UNICODE)


class SessionError(RuntimeError):
    """会话名对不上、或者这里有别的问题让人没法继续。"""


class SessionExists(SessionError):
    """这个名字已经有会话了（新建时撞名）。"""


def _exists_message(name: str) -> str:
    """撞名时给人的那句话。两处抛 `SessionExists` 共用——查到了、和抢输了，
    对人来说是同一件事，措辞不该分叉（网页壳把它映成 409，见 `web/server.py`）。"""
    return f"已经有叫 {name} 的会话了：换一个名字，或者直接切到那一条。"


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    model: str
    context_limit: int
    created_at: float
    last_active: float
    archived: bool = False
    title_source: str = ""


def meta_path(root: Path | str, session_id: str) -> Path:
    return Path(root) / ".agent" / "sessions" / safe(session_id) / META_NAME


def read_meta(root: Path | str, session_id: str) -> dict:
    payload = jsonstore.read_json(meta_path(root, session_id), {})
    return payload if isinstance(payload, dict) else {}


def write_meta(root: Path | str, session_id: str, **changes: Any) -> dict[str, Any]:
    """合并写：调用方只需要给要改的键。"""

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        data.setdefault("id", str(session_id))
        data.update(changes)
        return data

    return jsonstore.update_json(meta_path(root, session_id), mutate, {})


def _db(root: Path | str) -> sqlite3.Connection | None:
    """只读打开记忆库；坏了、没有，就返回 None（列表少几行，不炸）。"""
    path = Path(root) / ".agent" / "memory.db"
    if not path.is_file():
        return None
    try:
        conn = open_db(path)
        init_memory_schema(conn)
    except sqlite3.Error:
        return None
    return conn


def list_sessions(root: Path | str, include_archived: bool = False) -> list[Session]:
    """最近活跃在前。`memory.db` 给活跃与模型，`meta.json` 给标题与归档。"""
    conn = _db(root)
    rows = []
    if conn is not None:
        try:
            rows = db_sessions(conn)
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()

    found: list[Session] = []
    seen: set[str] = set()
    for row in rows:
        session_id = str(row["id"])
        seen.add(safe(session_id))
        meta = read_meta(root, session_id)
        archived = bool(meta.get("archived"))
        if archived and not include_archived:
            continue
        found.append(
            Session(
                id=session_id,
                title=str(meta.get("title") or ""),
                model=str(row["model"] or ""),
                context_limit=int(row["context_limit"] or 0),
                created_at=float(row["created_at"] or 0),
                last_active=float(row["last_active"] or 0),
                archived=archived,
                title_source=str(meta.get("title_source") or ""),
            )
        )

    # 只有 meta.json、还没跑过一轮的会话也要看得见（`create_session` 建的就是这种）。
    base = Path(root) / ".agent" / "sessions"
    if base.is_dir():
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name in seen or item.name.startswith("."):
                continue
            meta = jsonstore.read_json(item / META_NAME, {})
            if not isinstance(meta, dict):
                continue
            archived = bool(meta.get("archived"))
            if archived and not include_archived:
                continue
            found.append(
                Session(
                    id=str(meta.get("id") or item.name),
                    title=str(meta.get("title") or ""),
                    model="",
                    context_limit=0,
                    created_at=float(meta.get("created_at") or 0),
                    last_active=float(meta.get("last_active") or 0),
                    archived=archived,
                    title_source=str(meta.get("title_source") or ""),
                )
            )
    found.sort(key=lambda item: item.last_active, reverse=True)
    return found


def validate_session_name(name: str) -> str:
    """人点名的会话名：过不了就抛 `SessionError`（说人话，不冒栈）。

    规矩来自 `safe()`：它把认不出的字符换成 `_`、把首尾的 `.` / `_` 去掉。
    **凡是会被它改写的名字这里一律不收**——会改写就等于两个不同的名字落到
    同一个目录上，而"我以为建了一条新的、其实盖掉了旧那条"是最难查的一种。
    """
    raw = str(name)
    clean = raw.strip()
    if not clean:
        raise SessionError("会话名不能是空的。")
    if clean != raw:
        raise SessionError("会话名两边不能有空白。")
    if len(clean) > NAME_LIMIT:
        raise SessionError(
            f"会话名太长了：最多 {NAME_LIMIT} 个字符，现在 {len(clean)} 个。"
        )
    if not _NAME_SHAPE.match(clean) or safe(clean) != clean:
        raise SessionError(
            "会话名只能用中文、字母、数字、下划线、横线和点"
            "（不能有斜杠、空格、冒号或别的符号）"
            "——它同时也是 .agent/sessions/ 下的目录名。"
        )
    return clean


def has_session(root: Path | str, name: str) -> bool:
    """这个工作区里已经有叫这个名字的会话了没有。

    **已归档的也算**：归档只是从默认列表里藏起来，那一条的目录与记忆都还在——
    在它上面"新建"等于把它的 `meta.json` 覆盖掉。
    """
    raw = str(name).strip()
    if not raw:
        return False
    if meta_path(root, raw).is_file():
        return True
    # 只在记忆库里有行、目录还没建的那种也算：`create_session` 会往同一行
    # `record_session`，两条活于是共用一份记忆。
    return any(item.id == raw for item in list_sessions(root, include_archived=True))


def create_session(root: Path | str, title: str = "", session_id: str = "") -> str:
    """新建一条会话：id 默认带时间戳（可排序）+ 两位随机（不撞车）。

    网页壳的「＋ 新建会话」可以让人**点名**（`session_id`）：那时名字先过
    `validate_session_name`（空、路径分隔符、超长一律报错），而且**撞名不许**
    （抛 `SessionExists`）——`meta.json` 会被静默覆盖，而记忆库里那一行还在。
    不点名时**逐字保留老行为**（自动 id），`spool session new` 走的就是那条路。

    存储位置与命名与老路完全一样：`.agent/sessions/<safe(id)>/meta.json`
    加记忆库的一行；两条路都只经由这里，不另写一套建法。

    **点名的这条走独占创建（`jsonstore.create_json`），不只是"先查再写"**：
    查一下再写是有缝的——网页壳两个请求同时到达（双击、双标签页、脚本并发），
    两边都会在对方落盘**之前**查到"没有"，于是**都**返回 200，而磁盘上只剩
    后写的那一份标题：人看到的是"我建的两条会话变成了一条"。独占创建把裁决
    交给内核，同一个名字只有一次 `open` 能过，输的那个拿到 `SessionExists`
    （网页壳照旧映成 409），一个字节都不覆盖赢家的 `meta.json`。

    自动 id 那条**不动**：它的名字是时间戳 + 两位随机，撞名的概率与"人点了两次
    同一个名字"根本不是一个量级，而且它是既有行为（`spool session new`、桥、
    `spool chat --new` 都走它）——为它换建法等于顺手改一条没人报过问题的路。
    """
    wanted = str(session_id or "")
    now = time.time()
    if wanted:
        wanted = validate_session_name(wanted)
        if has_session(root, wanted):
            raise SessionExists(_exists_message(wanted))
        session_id = wanted
        initial = {
            "id": wanted,
            "created_at": now,
            "last_active": now,
            "title": str(title or ""),
        }
        if str(title or "").strip():
            initial["title_source"] = TITLE_MANUAL
        try:
            # 先建目录由 create_json 负责；这里只声明"这条会话的初始内容"。
            jsonstore.create_json(meta_path(root, wanted), initial)
        except FileExistsError:
            # 并发：另一个请求在"查过之后、写之前"把它建好了。明确失败，
            # 不覆盖——这正是这个函数存在的理由。
            raise SessionExists(_exists_message(wanted)) from None
    else:
        session_id = time.strftime("%y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        initial: dict[str, Any] = {
            "created_at": now,
            "last_active": now,
            "title": str(title or ""),
        }
        if str(title or "").strip():
            initial["title_source"] = TITLE_MANUAL
        write_meta(root, session_id, **initial)
    conn = _db(root)
    if conn is not None:
        try:
            record_session(conn, session_id, str(Path(root).resolve()))
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return session_id


def resolve_session(root: Path | str, name: str) -> str:
    """id / 序号（1 起）/ `latest`。**不认识就报错，不新建**。"""
    raw = str(name).strip()
    if not raw:
        raise SessionError("没有给会话名")
    items = list_sessions(root, include_archived=True)
    if raw == "latest":
        if not items:
            raise SessionError("这个工作区还没有任何会话。用 `spool session new` 建一条。")
        return items[0].id
    for item in items:
        if item.id == raw:
            return item.id
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(items):
            return items[index - 1].id
        raise SessionError(f"只有 {len(items)} 条会话，没有第 {index} 条。")
    listed = "\n".join(f"  {index}) {item.id}  {item.title or ''}" for index, item in enumerate(items, start=1))
    if not items:
        raise SessionError(f"没有叫 {raw} 的会话，这个工作区还没有任何会话。")
    raise SessionError(f"没有叫 {raw} 的会话。现在有：\n{listed}")


def rename_session(root: Path | str, session_id: str, title: str) -> None:
    resolve_session(root, session_id)  # 先在，才给改
    write_meta(
        root,
        session_id,
        title=str(title).strip(),
        title_source=TITLE_MANUAL,
    )


def archive_session(root: Path | str, session_id: str, archived: bool = True) -> None:
    resolve_session(root, session_id)
    write_meta(root, session_id, archived=bool(archived))


def clean_title(raw: str) -> str:
    """把模型/目标里的一句话收成一行标题。

    模型不总是照格式回：可能带引号、`标题：`、换行、结尾句号，或者干脆回一整段。
    这里只做展示前的清洗，不判断语义；空串表示"这不能当标题"。
    """
    raw_text = str(raw or "").strip()
    if not raw_text:
        return ""
    # 标题只取第一行：模型有时会在标题后面另起一行解释。
    text = " ".join(raw_text.splitlines()[0].split())
    if not text:
        return ""
    # 模型经常把标题包在引号里；引号不是标题的一部分。
    for mark in ('"', "'", "“", "”", "‘", "’"):
        text = text.replace(mark, "")
    text = re.sub(r"^(?:标题|会话标题|name|title)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" \t`#*-：:，,。.!！？?…")
    if not text:
        return ""
    # 第一句就够当标题了；后面的"另外/同时"属于正文。
    for sep in ("。", "！", "？", "\n", "；", ";"):
        at = text.find(sep)
        if at > 0:
            text = text[:at]
            break
    text = text.strip(" \t\"'“”‘’`#*-：:，,。.!！？?…")
    return text[:TITLE_LIMIT].strip()


def title_from_goal(goal: str) -> str:
    """标题取第一句目标，去掉"请/帮我"这类口令和 Markdown 前缀。

    这是**种子标题**：模型还没起名、或模型不可用时，左栏至少有一个可读的名字。
    真正的 Agent 标题走 `set_agent_title`，会覆盖 seed。
    """
    text = clean_title(goal)
    if not text:
        return ""
    # 只削掉一次开头口令；不递归，免得把"我想修"这种正常主语也吃掉。
    text = re.sub(
        r"^(?:(?:请你?|帮我|麻烦你?|我需要|我想|能不能|可以(?:帮我)?)"
        r"\s*[:：,，]?\s*)+",
        "",
        text,
    )
    text = text.strip(" \t\"'“”‘’`#*-：:，,。.!！？?…")
    return text[:TITLE_LIMIT].strip()


def ensure_title(root: Path | str, session_id: str, first_goal: str) -> str:
    """没标题才填，返回最终标题。已有标题不动——人是按标题认这条会话的。"""
    meta = read_meta(root, session_id)
    existing = str(meta.get("title") or "").strip()
    if existing:
        return existing
    title = title_from_goal(first_goal)
    if not title:
        return ""
    write_meta(root, session_id, title=title, title_source=TITLE_SEED)
    return title


def set_agent_title(root: Path | str, session_id: str, title: str) -> str:
    """写入 Agent 起的标题；手工标题与来源不明的老标题一律不动。

    返回最终标题。生成失败时返回当前标题（可能是空串），调用方不需要区分
    "没生成"与"已有标题"——两者都不该弹错误。
    """
    meta = read_meta(root, session_id)
    current = str(meta.get("title") or "").strip()
    source = str(meta.get("title_source") or "").strip()
    if current and source not in (TITLE_SEED, TITLE_AGENT):
        return current
    cleaned = clean_title(title)
    if not cleaned:
        return current
    write_meta(root, session_id, title=cleaned, title_source=TITLE_AGENT)
    return cleaned
