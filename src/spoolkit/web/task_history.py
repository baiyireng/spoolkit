"""跨会话任务历史的只读目录。

只读当前工作区 `.agent/task-events` 里直接放下的 `*.jsonl`。
文件名的 stem 就是 session id。一行一个任务事件字典，坏行跳过。
分组复用 `build_task_groups`，再给每组盖上 `session`。

这里不生成、不改写事件文件，也不碰当前 Runner 的内存快照。
调用方不传路径：扫哪一个目录由工作区根决定，请求里的路径用不上。
"""

from __future__ import annotations

import json
from pathlib import Path

from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.session_state import safe
from spoolkit.web.task_view import build_task_groups

_SUFFIX = ".jsonl"


def read_task_history(root: Path) -> dict:
    """返回 `{sessions, groups}`。groups 按最近写入优先。读失败给空目录。"""
    files = _event_files(Path(root) / ".agent" / "task-events")
    sessions: list[tuple[int, str, dict]] = []
    groups: list[tuple[int, int, dict]] = []
    for path, mtime in files:
        text = _read_text(path)
        if text is None:
            continue
        session_id = path.name[: -len(_SUFFIX)]
        kept, last_line = _parse_events(text)
        for group in build_task_groups(kept):
            task_id = str(group.get("task_id") or "")
            groups.append(
                (
                    mtime,
                    last_line.get(task_id, -1),
                    {**group, "session": session_id},
                )
            )
        sessions.append(
            (
                mtime,
                path.name,
                {"id": session_id, "title": _title(root, session_id)},
            )
        )
    sessions.sort(key=lambda item: (item[0], item[1]), reverse=True)
    groups.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {
        "sessions": [item[2] for item in sessions],
        "groups": [item[2] for item in groups],
    }


def _event_files(base: Path) -> list[tuple[Path, int]]:
    """只要这一层的普通 `.jsonl`。符号链接、子目录、别的后缀都不跟。"""
    try:
        if not base.is_dir():
            return []
        base_resolved = base.resolve()
        entries = list(base.iterdir())
    except OSError:
        return []
    found: list[tuple[Path, int]] = []
    for path in entries:
        if not _is_event_file(base_resolved, path):
            continue
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        found.append((path, mtime))
    found.sort(key=lambda item: (item[1], item[0].name))
    return found


def _is_event_file(base_resolved: Path, path: Path) -> bool:
    name = path.name
    if not name.endswith(_SUFFIX) or name.startswith("."):
        return False
    stem = name[: -len(_SUFFIX)]
    if not stem or stem != safe(stem) or ".." in stem or "/" in stem or "\\" in stem:
        return False
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.parent.resolve() != base_resolved:
            return False
        path.resolve().relative_to(base_resolved)
    except (OSError, ValueError):
        return False
    return True


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _parse_events(text: str) -> tuple[list[dict], dict[str, int]]:
    """坏行跳过。行号留给排序：同一文件里越靠后越新。"""
    kept: list[dict] = []
    last_line: dict[str, int] = {}
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        kept.append(data)
        last_line[str(data.get("task_id") or "")] = index
    return kept, last_line


def _title(root: Path, session_id: str) -> str:
    """标题在 session meta 里。读不到、空着，就用 session id。"""
    try:
        meta = kernel_sessions.read_meta(root, session_id)
        title = str(meta.get("title") or "").strip() if isinstance(meta, dict) else ""
    except (OSError, ValueError, TypeError):
        return session_id
    return title or session_id
