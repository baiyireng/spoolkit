"""会话管理命令。

建了会话却管不了它，等于没有会话概念——你甚至不知道自己建过哪些。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from spoolkit.memory.store import init_memory_schema, list_sessions
from spoolkit.store.db import open_db


def _stamp(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M")


def session_command(args: argparse.Namespace) -> int:
    """列出这个工作区里的会话。"""
    project_root = Path(args.root).resolve()
    db_path = project_root / ".agent" / "memory.db"
    if not db_path.exists():
        print("这个工作区还没有任何会话。", file=sys.stderr)
        return 2

    conn = open_db(db_path)
    init_memory_schema(conn)
    rows = list_sessions(conn)
    if not rows:
        print("还没有会话记录。", file=sys.stderr)
        return 2

    print(f"{'会话':<16}{'模型':<24}{'窗口':>7}{'最近活跃':>14}")
    for row in rows:
        print(
            f"{row['id']:<16}{row['model'] or '—':<24}"
            f"{row['context_limit']:>7}{_stamp(row['last_active']):>14}"
        )

    print()
    print(f"共 {len(rows)} 个会话。用 --session <名字> 指定要进哪个。")
    conn.close()
    return 0

