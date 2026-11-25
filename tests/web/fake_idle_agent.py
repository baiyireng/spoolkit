"""测试用的假 agent：**照 `run.py` 的次序**记一条用户往来，然后一直待着等 terminate。

为什么要记那条往来：真实路径上，子进程开工第一件事就是把用户这句话写进
memory.db，收尾时再写助手那条。手动中断要量的"已产出的内容保留"与"思考过程事后
还能回看"，量的正是这两样——不写的话，停掉之后根本没有东西可看。

用法：`python fake_idle_agent.py <memory.db 路径> <会话名> <目标>`
"""

import json
import sqlite3
import sys
import time

db, session, goal = sys.argv[1], sys.argv[2], sys.argv[3]


def emit(**payload) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


conn = sqlite3.connect(db, timeout=5)
try:
    conn.execute(
        "INSERT INTO transcript(session_id, role, content, meta, created_at)"
        " VALUES (?,?,?,?,?)",
        (session, "user", goal, "", time.time()),
    )
    conn.commit()
finally:
    conn.close()

emit(type="start", session=session, goal=goal)
emit(type="step", n=1)
# 一直待着：`/run/stop` 的那一下才有东西可停。0.05 秒一拍不占 CPU，也让
# terminate 之后它**收得住**（不必走到 kill 那一步）。
while True:
    time.sleep(0.05)
