"""机器级小文件的读写。

为什么单独一层：登记表、客户端指向、放行都是"读-改-写"的小文件，且可能被多个
进程同时碰（CLI 一个、桥一个、hub 一个）。规则收在一处，别让每个模块自己写一遍。

**不做跨进程文件锁**：改这些文件的动作（切工作区、批准放行）都是人在按，本来
就不是并发写，为它引入一套锁协议不划算。只做两件便宜的事——进程内加锁（挡住
同一进程的多个线程，网页壳就有），以及"写临时文件再原子替换"（挡住读到半份）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def read_json(path: Path, default: Any) -> Any:
    """读不到、坏了、不是 JSON 对象，一律给默认值——它们是便利，不是数据。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return payload


def write_json(path: Path, payload: Any) -> None:
    """写失败会抛 OSError（调用方决定是吞掉还是报给人）。"""
    path = Path(path)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # 临时文件留下会被人当成"状态文件"翻，清掉再抛。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def update_json(path: Path, mutate: Callable[[Any], Any], default: Any) -> Any:
    """读-改-写，整段在进程内锁里完成。"""
    path = Path(path)
    with _lock_for(path):
        updated = mutate(read_json(path, default))
        write_json(path, updated)
        return updated
