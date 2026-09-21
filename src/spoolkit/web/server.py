"""HTTP 服务与 SSE 广播。

必须用 ThreadingHTTPServer：默认的单线程服务会让一个 SSE 长连接
把整个服务堵死——连首页都打不开，而你会以为服务崩了。

SSE 每写完一条都要 flush：不 flush 的话事件攒在缓冲里，
界面看起来像卡住。
"""

import json
import queue
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from spoolkit.health import describe_change, describe_service_change, probe_from_extra, probe_services
from spoolkit.web.page import HTML
from spoolkit.web.runner import Runner

KEEPALIVE_SECONDS = 15
MAX_BODY = 64 * 1024
HEALTH_POLL_SECONDS = 5.0


def _handler_for(runner: Runner):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            """默认会往 stderr 刷每一行请求，本地调试时全是噪音。"""

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        # --- GET ---

        def do_GET(self) -> None:  # noqa: N802 - 基类约定的方法名
            if self.path == "/":
                self._html(HTML)
            elif self.path == "/state":
                self._json(200, runner.snapshot())
            elif self.path == "/events":
                self._stream()
            else:
                self._json(404, {"error": "没有这个路径"})

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # 首帧给完整状态：EventSource 重连不会补发断线期间的事件，
            # 不给快照的话页面会停在断线前那一刻，而运行其实还在继续。
            self._push({"type": "state", **runner.snapshot()})
            listener = runner.subscribe()
            try:
                while True:
                    try:
                        event = listener.get(timeout=KEEPALIVE_SECONDS)
                    except queue.Empty:
                        # 心跳：代理和浏览器会掐掉长时间没数据的连接。
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._push({"type": event.type, **event.data})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                runner.unsubscribe(listener)

        def _push(self, payload: dict) -> None:
            line = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        # --- POST ---

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/run":
                payload = self._body()
                goal = str(payload.get("goal") or "").strip()
                if not goal:
                    self._json(400, {"error": "缺少 goal"})
                    return
                if not runner.start(goal):
                    self._json(409, {"error": "已有运行在进行，等它结束再发"})
                    return
                self._json(202, {"ok": True})
                return

            if self.path == "/confirm":
                payload = self._body()
                if not runner.confirm(bool(payload.get("apply"))):
                    self._json(409, {"error": "当前没有待确认的改动"})
                    return
                self._json(200, {"ok": True})
                return

            self._json(404, {"error": "没有这个路径"})

    return Handler


def build_server(runner: Runner, host: str, port: int) -> ThreadingHTTPServer:
    """构造服务。port 传 0 表示由系统分配。"""
    return ThreadingHTTPServer((host, port), _handler_for(runner))


def serve(
    project_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    session: str = "cli",
    extra_args: Sequence[str] = (),
    extra_health: Callable[[], dict] | None = None,
) -> None:
    """启动服务，直到被中断。"""
    runner = Runner(
        project_root,
        session=session,
        extra_args=list(extra_args),
        extra_health=extra_health,
    )
    httpd = build_server(runner, host, port)
    print(f"Web UI: http://{host}:{httpd.server_port}/")
    stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_health,
        args=(list(extra_args), extra_health, stop),
        daemon=True,
        name="spool-health",
    )
    watcher.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


def _watch_health(
    extra_args: list[str],
    extra_health: Callable[[], dict] | None,
    stop: threading.Event,
) -> None:
    """通↔断时往终端打一行。页面横幅靠 /state，终端靠这一头。"""
    last_model: dict | None = None
    last_bridge: bool | None = None
    last_services: dict[str, bool] = {}
    while True:
        try:
            model = probe_from_extra(extra_args)
        except Exception as exc:
            model = {
                "ok": False,
                "target": "",
                "detail": f"探活失败：{exc}",
            }
        line = describe_change(last_model, model)
        if line:
            print(line, file=sys.stderr)
        last_model = model
        try:
            rows = probe_services()
        except Exception:
            rows = []
        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            prev = last_services.get(name)
            last_services[name] = bool(row.get("ok"))
            note = describe_service_change(
                None if prev is None else {"ok": prev},
                row,
            )
            if note:
                print(note, file=sys.stderr)
        if extra_health is not None:
            try:
                extra = extra_health() or {}
            except Exception:
                extra = {}
            bridge = extra.get("bridge") if isinstance(extra, dict) else None
            if isinstance(bridge, dict):
                alive = bool(bridge.get("ok"))
                if last_bridge is not None and alive != last_bridge:
                    target = str(bridge.get("target") or "").strip()
                    where = f"（{target}）" if target else ""
                    print(
                        ("通道在跑" if alive else "通道已退出") + where,
                        file=sys.stderr,
                    )
                last_bridge = alive
        if stop.wait(HEALTH_POLL_SECONDS):
            break

