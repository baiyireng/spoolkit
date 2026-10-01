"""机器级小文件的读写。

为什么单独一层：登记表、客户端指向、放行都是"读-改-写"的小文件，且可能被多个
进程同时碰（CLI 一个、桥一个、hub 一个）。规则收在一处，别让每个模块自己写一遍。

**不做跨进程文件锁**：改这些文件的动作（切工作区、批准放行）都是人在按，本来
就不是并发写，为它引入一套锁协议不划算。只做两件便宜的事——进程内加锁（挡住
同一进程的多个线程，网页壳就有），以及"写临时文件再原子替换"（挡住读到半份）。

**唯一的例外是"只能有一个"的那种创建**（见 `create_json`）：同一时刻用
`update_json` 先查再写，两个进程会**都**通过检查、**都**落盘，后写的那个
把先写的盖掉，而两边都以为建成了。`O_EXCL` 让内核来判"谁先创建"，
恰好一个过；这不是锁协议，是一次性的抢占，代价只有一次 `os.open`。
"""

from __future__ import annotations

import contextlib
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


def create_json(path: Path, payload: Any) -> None:
    """独占创建：文件已经存在就抛 `FileExistsError`，**绝不覆盖**。

    与 `write_json` 的差别只有一条，但它要命：这里是"我建，且只有我能建成"，
    不是"我说了算"。两个进程同时"先查没有、再写"时，`write_json` 会让**两个
    都成功**（后写的赢），调用方于是都以为建成了——网页壳上就是"两个人都说
    建好了，磁盘上只剩一份"。`O_EXCL` 把判定交给内核：同一个路径只有一次
    `open` 能过，其余的拿到 `FileExistsError`，调用方据此明确失败。

    写失败时把刚建出来的那个文件删掉再抛：留一个半份 JSON 在那儿，后面的
    创建会永远撞 `FileExistsError`，而文件内容读出来是默认值——那是最难查的
    一种"建不起来"。删不掉就算了（受限环境里 unlink 可能被拒），原始错误更重要。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
