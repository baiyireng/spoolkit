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

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from spoolkit.kernel import jsonstore
from spoolkit.memory.store import init_memory_schema, list_sessions as db_sessions
from spoolkit.memory.store import record_session
from spoolkit.session_state import safe
from spoolkit.store.db import open_db

META_NAME = "meta.json"
TITLE_LIMIT = 40


class SessionError(RuntimeError):
    """会话名对不上、或者这里有别的问题让人没法继续。"""


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    model: str
    context_limit: int
    created_at: float
    last_active: float
    archived: bool = False


def meta_path(root: Path | str, session_id: str) -> Path:
    return Path(root) / ".agent" / "sessions" / safe(session_id) / META_NAME


def read_meta(root: Path | str, session_id: str) -> dict:
    payload = jsonstore.read_json(meta_path(root, session_id), {})
    return payload if isinstance(payload, dict) else {}


def write_meta(root: Path | str, session_id: str, **changes) -> dict:
    """合并写：调用方只需要给要改的键。"""

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        data.setdefault("id", str(session_id))
        data.update(changes)
        return data

    return jsonstore.update_json(meta_path(root, session_id), mutate, {})


def _db(root: Path | str):
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
                )
            )
    found.sort(key=lambda item: item.last_active, reverse=True)
    return found


def create_session(root: Path | str, title: str = "") -> str:
    """新建一条会话：id 带时间戳（可排序）+ 两位随机（不撞车）。"""
    session_id = time.strftime("%y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    now = time.time()
    write_meta(root, session_id, created_at=now, last_active=now, title=str(title or ""))
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
    write_meta(root, session_id, title=str(title).strip())


def archive_session(root: Path | str, session_id: str, archived: bool = True) -> None:
    resolve_session(root, session_id)
    write_meta(root, session_id, archived=bool(archived))


def title_from_goal(goal: str) -> str:
    """标题取第一句目标，截断到 TITLE_LIMIT。空白目标给空串。"""
    text = " ".join(str(goal).split())
    return text[:TITLE_LIMIT]


def ensure_title(root: Path | str, session_id: str, first_goal: str) -> None:
    """没标题才填。已有标题不动——人是按标题认这条会话的。"""
    meta = read_meta(root, session_id)
    if str(meta.get("title") or "").strip():
        return
    title = title_from_goal(first_goal)
    if not title:
        return
    write_meta(root, session_id, title=title)
