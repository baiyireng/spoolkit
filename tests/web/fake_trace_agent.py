"""测试用的假 agent：吐一轮**带失败痕迹**的事件，并按 `run.py` 的约定写两条往来。

为什么单独一个脚本、而不是在用例里 `runner._absorb(...)`：
"思考过程事后还能回看"这件事跨越了两处——子进程**发出来的事件**（step / tool / final）
与它**写进 memory.db 的往来**（用户那条、助手那条）。服务端是按时间窗口把两者认领到
一起的（`web/runner.py::attach_traces`），只有两边都真的发生，那条路才被走到。

用法：`python fake_trace_agent.py <memory.db 路径> <会话名> <目标>`
"""

import json
import sqlite3
import sys
import time

db, session, goal = sys.argv[1], sys.argv[2], sys.argv[3]


def emit(**payload) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def record(role: str, content: str, meta: str = "") -> None:
    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.execute(
            "INSERT INTO transcript(session_id, role, content, meta, created_at)"
            " VALUES (?,?,?,?,?)",
            (session, role, content, meta, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


emit(type="start", session=session, goal=goal)
# 与 `cli/commands/run.py` 同一个次序：开工先记用户那条，收尾前记助手那条。
record("user", goal)
emit(type="step", n=1)
emit(type="tool", name="read_file", ok=False, detail="文件不存在：src/a.py")
emit(type="note", text="督导：这一步没过去", ok=False)
time.sleep(0.05)
record("assistant", "没做成：文件不存在", meta="1 步，未完成")
emit(
    type="final",
    ok=False,
    text="没做成：文件不存在",
    error={"stage": "model", "kind": "connect", "url": "http://127.0.0.1:9"},
)
