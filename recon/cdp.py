"""Tiny dependency-free CDP client over raw WebSocket (stdlib only).

Imported by the browser tests in this worktree. Talks to a Chromium/Edge
started with --remote-debugging-port.
"""
import base64
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse


class WebSocket:
    def __init__(self, url, timeout=15):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self.buf = b""
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws handshake closed")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self.buf = rest
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("ws handshake failed: " + head.decode("latin1", "replace"))
        self.fragment = b""
        self.fragment_op = 0

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise RuntimeError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv(self):
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x8:
                raise RuntimeError("ws closed by peer")
            if op == 0x1:
                self.fragment = payload
                self.fragment_op = 1
                if fin:
                    return self.fragment.decode("utf-8")
                continue
            if op == 0x0:
                self.fragment += payload
                if fin:
                    text = self.fragment.decode("utf-8")
                    self.fragment = b""
                    return text
                continue
            # binary etc: ignore


class CDP:
    def __init__(self, port=9223):
        self.port = port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last = None
        for _ in range(80):
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    targets = json.loads(resp.read().decode())
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    self.page = pages[0]
                    self.ws = WebSocket(self.page["webSocketDebuggerUrl"])
                    break
            except Exception as exc:  # noqa: BLE001
                last = exc
                import time

                time.sleep(0.25)
        else:
            raise RuntimeError(f"cannot connect to CDP: {last!r}")
        self.next_id = 0
        self.events = []

    def cmd(self, method, params=None, timeout=30):
        self.next_id += 1
        want = self.next_id
        self.ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
        self.ws.sock.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def eval(self, expression, timeout=30):
        result = self.cmd(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
        return result.get("result", {}).get("value")

    def shot(self, path):
        result = self.cmd("Page.captureScreenshot", {"format": "png", "fromSurface": True}, timeout=60)
        data = base64.b64decode(result["data"])
        with open(path, "wb") as fh:
            fh.write(data)
        return len(data)

    def viewport(self, width, height, mobile=False):
        self.cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": mobile,
                "screenWidth": width,
                "screenHeight": height,
            },
        )

    def media(self, scheme="dark"):
        self.cmd(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": scheme}]},
        )

    def navigate(self, url):
        self.cmd("Page.navigate", {"url": url})

    def wait_ready(self, timeout=20):
        import time

        end = time.time() + timeout
        while time.time() < end:
            try:
                state = self.eval(
                    "({ready: document.readyState, spool: (typeof lastState !== 'undefined' && lastState !== null),"
                    " col: !!document.querySelector('#stream .col')})"
                )
                if state and state.get("ready") == "complete" and state.get("spool"):
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        return False
