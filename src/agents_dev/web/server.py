"""HTTP 服务与 SSE 广播。

必须用 ThreadingHTTPServer：默认的单线程服务会让一个 SSE 长连接
把整个服务堵死——连首页都打不开，而你会以为服务崩了。

SSE 每写完一条都要 flush：不 flush 的话事件攒在缓冲里，
界面看起来像卡住。
"""

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from agents_dev.web.page import HTML
from agents_dev.web.runner import Runner

KEEPALIVE_SECONDS = 15
MAX_BODY = 64 * 1024


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
) -> None:
    """启动服务，直到被中断。"""
    runner = Runner(project_root, session=session, extra_args=list(extra_args))
    httpd = build_server(runner, host, port)
    print(f"Web UI: http://{host}:{httpd.server_port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

