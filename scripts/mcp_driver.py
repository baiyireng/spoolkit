"""扮演外部 agent，通过 MCP stdio 驱动 spoolkit agent 干活。

用法：
    python scripts/mcp_driver.py --root D:/workSpace/scratch_mcp --goal "..." [--policy ask]

它启动 `spool mcp` 子进程，走完整的 MCP 握手 → 下任务 → 轮询事件 →
遇到待确认改动就自动 apply（模拟用户按"应用"）→ 直到任务结束。
把全程的事件打印出来，用来观察 MCP 协作路径的真实行为。
"""
import argparse
import json
import subprocess
import sys
import time
import threading
import queue


def send(proc, payload):
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--policy", default="ask")
    ap.add_argument("--scope", default="**")
    ap.add_argument("--mode", default="task")
    ap.add_argument("--session", default="mcp-test")
    ap.add_argument("--web", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    cmd = [
        "spool", "mcp",
        "--root", args.root,
        "--scope", args.scope,
        "--policy", args.policy,
        "--session", args.session,
        "--no-setup",
    ]
    if args.web:
        cmd.append("--web")
    else:
        cmd.append("--no-web")

    print(f"[driver] 启动: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    # stderr 单独排空，避免子进程 stderr 撑爆管道阻塞
    def drain_stderr():
        for line in proc.stderr:
            sys.stderr.write(f"[mcp-stderr] {line}")
    threading.Thread(target=drain_stderr, daemon=True).start()

    rid = [0]

    def next_id():
        rid[0] += 1
        return rid[0]

    def call(method, params=None):
        i = next_id()
        send(proc, {"jsonrpc": "2.0", "id": i, "method": method,
                     "params": params or {}})
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("mcp 进程 stdout EOF")
            msg = json.loads(line)
            if msg.get("id") == i:
                return msg
            # 进度通知等，打印一下
            if msg.get("method") == "notifications/progress":
                p = msg.get("params", {})
                print(f"  [进度 #{p.get('progress')}] {p.get('message')}", flush=True)

    # 1. 握手
    print("[driver] → initialize", flush=True)
    r = call("initialize")
    print(f"[driver] ← {json.dumps(r.get('result'), ensure_ascii=False)[:200]}", flush=True)

    # 2. 列工具
    print("[driver] → tools/list", flush=True)
    r = call("tools/list")
    tools = r.get("result", {}).get("tools", [])
    print(f"[driver] ← {len(tools)} 个工具: {[t['name'] for t in tools]}", flush=True)

    # 3. 看工作区
    print("[driver] → workspace_status", flush=True)
    r = call("tools/call", {"name": "workspace_status"})
    print(f"[driver] ← {r.get('result', {}).get('content', [{}])[0].get('text', '')[:300]}", flush=True)

    # 4. 下任务
    print(f"[driver] → delegate_task (mode={args.mode})", flush=True)
    print(f"  goal: {args.goal[:80]}", flush=True)
    r = call("tools/call", {"name": "delegate_task",
                            "arguments": {"goal": args.goal, "mode": args.mode}})
    print(f"[driver] ← {r.get('result', {}).get('content', [{}])[0].get('text', '')[:300]}", flush=True)

    # 5. 轮询事件 + 自动确认
    last_seq = 0
    start = time.time()
    confirmed_this_round = False
    while time.time() - start < args.timeout:
        time.sleep(2)
        r = call("tools/call", {"name": "task_status"})
        status_text = r.get("result", {}).get("content", [{}])[0].get("text", "")
        status = json.loads(status_text) if status_text else {}
        running = status.get("running", False)
        awaiting = status.get("awaiting", 0)
        done = status.get("done", False)
        ok = status.get("ok", None)
        print(f"[driver] status: running={running} awaiting={awaiting} done={done} ok={ok}", flush=True)

        # 拉新事件
        r = call("tools/call", {"name": "task_events", "arguments": {"since": last_seq}})
        ev_text = r.get("result", {}).get("content", [{}])[0].get("text", "")
        ev_data = json.loads(ev_text) if ev_text else {}
        events = ev_data.get("events", [])
        for ev in events:
            seq = ev.get("seq", "?")
            kind = ev.get("type", "?")
            # 事件数据展开在顶层（seq/type 之外的字段就是 data）
            data = {k: v for k, v in ev.items() if k not in ("seq", "type")}
            print(f"  [ev#{seq}] {kind}: {json.dumps(data, ensure_ascii=False)[:200]}", flush=True)
            last_seq = max(last_seq, seq)

        # 有待确认就自动 apply（模拟用户审批）
        if awaiting > 0 and not confirmed_this_round:
            print(f"[driver] → confirm_changes(apply=True)  模拟用户按应用", flush=True)
            r = call("tools/call", {"name": "confirm_changes", "arguments": {"apply": True}})
            print(f"[driver] ← {r.get('result', {}).get('content', [{}])[0].get('text', '')}", flush=True)
            confirmed_this_round = True
        if awaiting == 0:
            confirmed_this_round = False

        if done or not running:
            print(f"[driver] 任务结束: ok={ok}", flush=True)
            break
    else:
        print(f"[driver] 超时（{args.timeout}s）", flush=True)

    proc.stdin.close()
    proc.wait(timeout=5)
    print("[driver] 完成", flush=True)


if __name__ == "__main__":
    main()
