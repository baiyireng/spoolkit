"""把已经落库的任务事件收成给会话看的任务卡。

不改事件怎么生成、怎么落盘。这里只分组、推导状态，并按轮次挂到消息上。
卡片只用 summary / refs / artifacts。事件里没有 raw，这里也不补。
"""

from collections.abc import Sequence

from spoolkit.memory.transcript import ASSISTANT

# 视图只抄这些字段。raw、thought 即使混进字典也不往外带。
_EVENT_FIELDS = (
    "task_id",
    "run_id",
    "turn_id",
    "seq",
    "step",
    "phase",
    "source",
    "tool_call_id",
    "intent",
    "args_digest",
    "status",
    "summary",
    "refs",
    "artifacts",
    "duration",
    "risk",
)


def build_task_groups(events: Sequence[dict]) -> list[dict]:
    """按 task_id 分组，组内按 seq 排。出现顺序就是组的顺序。"""
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for item in events:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or "")
        if task_id not in buckets:
            order.append(task_id)
            buckets[task_id] = []
        buckets[task_id].append(_view_event(item))
    groups = []
    for task_id in order:
        rows = sorted(buckets[task_id], key=lambda row: (int(row["seq"]), int(row["step"])))
        first = rows[0]
        groups.append(
            {
                "task_id": task_id,
                "run_id": str(first.get("run_id") or ""),
                "turn_id": str(first.get("turn_id") or ""),
                "goal": _goal(rows),
                "status": task_status(rows),
                "events": rows,
                "artifacts": _artifacts(rows),
            }
        )
    return groups


def task_status(events: Sequence[dict]) -> str:
    """从这一组事件推出一张卡的状态。

    cancelled 优先：用户停掉就是停掉。
    其次 failed：有 phase=error，或 final 自己是 error。
    verify 失败但 final 仍是 ok，单独算 verify_failed。
    final ok 且上面都没有，才是 done。
    没有 final、也没有 error，就是还在跑。
    """
    if any(row.get("phase") == "final" and row.get("status") == "cancelled" for row in events):
        return "cancelled"
    if any(
        row.get("phase") == "error" or (row.get("phase") == "final" and row.get("status") == "error")
        for row in events
    ):
        return "failed"
    verify_error = any(row.get("phase") == "verify" and row.get("status") == "error" for row in events)
    final_ok = any(row.get("phase") == "final" and row.get("status") == "ok" for row in events)
    if verify_error and final_ok:
        return "verify_failed"
    if final_ok:
        return "done"
    return "running"


def attach_task_groups(messages: list[dict], turns: Sequence[dict], groups: Sequence[dict]) -> None:
    """把任务卡挂到这一轮的助手消息上。每张卡只挂一次。

    认领方式和轨迹一样：轮次窗口里的消息归这一轮，有助手回复就挂它。
    窗口里放进哪些 turn_id，是这一轮收到事件时记下来的，不按路径猜。
    挂不上的卡仍留在顶层 task_groups，靠 turn_id 还能对上轮次。
    """
    for item in messages:
        item["tasks"] = []
    by_turn: dict[str, list[dict]] = {}
    for group in groups:
        by_turn.setdefault(str(group.get("turn_id") or ""), []).append(group)
    mounted: set[int] = set()
    for turn in turns:
        ids = {str(item) for item in (turn.get("turn_ids") or []) if str(item)}
        if not ids:
            continue
        start = float(turn.get("started_at") or 0.0)
        end = float(turn.get("ended_at") or 0.0)
        mine = []
        for item in messages:
            at = item.get("created_at")
            if at is None:
                continue
            if start <= float(at) <= end:
                mine.append(item)
        if not mine:
            continue
        holder = next(
            (item for item in reversed(mine) if item.get("role") == ASSISTANT),
            mine[-1],
        )
        for turn_id in ids:
            for group in by_turn.get(turn_id, []):
                if id(group) in mounted:
                    continue
                holder["tasks"].append(group)
                mounted.add(id(group))


def _view_event(item: dict) -> dict:
    event: dict = {}
    for key in _EVENT_FIELDS:
        if key == "refs":
            event[key] = [str(ref) for ref in (item.get("refs") or [])]
        elif key == "artifacts":
            event[key] = _artifact_rows(item.get("artifacts") or [])
        elif key in ("seq", "step"):
            event[key] = int(item.get(key) or 0)
        elif key == "duration":
            event[key] = item.get("duration")
        else:
            event[key] = item.get(key) if item.get(key) is not None else ""
    return event


def _goal(events: Sequence[dict]) -> str:
    for row in events:
        if row.get("phase") == "understand":
            return str(row.get("summary") or "")
    return ""


def _artifacts(events: Sequence[dict]) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in events:
        for item in row.get("artifacts") or []:
            key = (str(item.get("type") or ""), str(item.get("path") or ""), str(item.get("ref") or ""))
            if key in seen:
                continue
            seen.add(key)
            found.append({"type": key[0], "path": key[1], "ref": key[2]})
    return found


def _artifact_rows(items: Sequence) -> list[dict]:
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "type": str(item.get("type") or ""),
                "path": str(item.get("path") or ""),
                "ref": str(item.get("ref") or ""),
            }
        )
    return rows
