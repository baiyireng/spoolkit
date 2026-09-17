from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSight:
    name: str
    path: str = ""
    query: str = ""
    content_hash: str = ""
    ok: bool = True


@dataclass(frozen=True)
class MonitorReport:
    cycle: str
    oscillations: int
    same_query: int


def normalize_query(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text.casefold(), flags=re.UNICODE)


def name_cycle(names: Sequence[str], window: int = 12) -> str:
    seq = list(names[-window:])
    for period in (2, 3):
        if len(seq) >= period * 2 and seq[-period * 2 : -period] == seq[-period:]:
            return "→".join(seq[-period:])
    return ""


def path_oscillations(sights: Sequence[ToolSight]) -> int:
    by_path: dict[str, list[str]] = defaultdict(list)
    for sight in sights:
        # 只有 write/replace 才有 content_hash；读文件的空 hash 不参与振荡判定。
        if sight.path and sight.content_hash:
            by_path[sight.path].append(sight.content_hash)

    count = 0
    for hashes in by_path.values():
        for i in range(len(hashes) - 2):
            if hashes[i] == hashes[i + 2] and hashes[i] != hashes[i + 1]:
                count += 1
    return count


def same_query_count(sights: Sequence[ToolSight]) -> int:
    counts: dict[str, int] = defaultdict(int)
    for sight in sights:
        normalized = normalize_query(sight.query)
        if normalized:
            counts[normalized] += 1
    return max(counts.values(), default=0)


def inspect(sights: Sequence[ToolSight], *, window: int = 12) -> MonitorReport:
    names = [sight.name for sight in sights]
    return MonitorReport(
        cycle=name_cycle(names, window=window),
        oscillations=path_oscillations(sights),
        same_query=same_query_count(sights),
    )
