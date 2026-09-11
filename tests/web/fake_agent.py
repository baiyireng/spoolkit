"""测试用的假 agent。

发一条 diff 与 await，读一行 stdin，再发 final。

用它而不是真 agent：确认流程的时序是这块最容易出错的地方，而真 agent
要跑几十秒、还要联网。假 agent 把时序压到毫秒级，能反复跑。
"""

import json
import sys


def emit(**payload) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


emit(type="diff", path="a.py", text="+新\n-旧")
emit(type="await", count=1)
answer = (sys.stdin.readline() or "").strip()
emit(type="confirm", applied=answer == "y")
emit(type="final", ok=True, text="收到 " + answer)
