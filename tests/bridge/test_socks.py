"""经 SOCKS5 出网：QQ 平台有 IP 白名单，借一台云主机出去能把白名单固定下来。

验的是我们自己的那段握手（不用第三方库）：一个假的 SOCKS5 服务端，
看它有没有按规矩收到 CONNECT、被拒时会不会报清楚。
"""

import socket
import threading

import pytest

from spoolkit.bridge.ws import WebSocket, WebSocketError, _parse_proxy, socks5_connect


class _FakeSocks:
    """最小 SOCKS5 服务端：只做无认证 + CONNECT。"""

    def __init__(self, reply_code: int = 0) -> None:
        self.reply_code = reply_code
        self.targets: list[tuple[str, int]] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        try:
            greeting = conn.recv(3)
            if greeting[:2] != b"\x05\x01":
                return
            conn.sendall(b"\x05\x00")
            head = conn.recv(4)
            if head[3] == 1:
                packed = conn.recv(4)
                host = socket.inet_ntoa(packed)
            else:
                size = conn.recv(1)[0]
                host = conn.recv(size).decode()
            port = int.from_bytes(conn.recv(2), "big")
            self.targets.append((host, port))
            conn.sendall(
                b"\x05" + bytes([self.reply_code]) + b"\x00\x01"
                + socket.inet_aton("0.0.0.0") + (0).to_bytes(2, "big")
            )
        except OSError:
            pass
        finally:
            conn.close()


def test_握手成功并说出目标() -> None:
    proxy = _FakeSocks()
    sock = socks5_connect("127.0.0.1", proxy.port, "api.sgroup.qq.com", 443)
    sock.close()
    assert proxy.targets == [("api.sgroup.qq.com", 443)]


def test_域名目标用_ATYP_3() -> None:
    """目标写成域名时（QQ 的网关就是这么给的）要用域名形式发过去。"""
    proxy = _FakeSocks()
    socks5_connect("127.0.0.1", proxy.port, "example.com", 8443)
    assert proxy.targets == [("example.com", 8443)]


def test_被拒时报清楚() -> None:
    proxy = _FakeSocks(reply_code=2)  # 规则不允许
    with pytest.raises(WebSocketError, match="SOCKS5 拒绝"):
        socks5_connect("127.0.0.1", proxy.port, "example.com", 443)


def test_代理地址写得不对要说人话() -> None:
    assert _parse_proxy("socks5://127.0.0.1:1080") == ("127.0.0.1", 1080)
    assert _parse_proxy("127.0.0.1:1080") == ("127.0.0.1", 1080)
    with pytest.raises(WebSocketError, match="看不懂"):
        _parse_proxy("只是主机名")


def test_没给代理时就直连() -> None:
    """不配代理的人不该受到任何影响（走的是原来的直连分支）。"""
    ws = WebSocket("ws://127.0.0.1:1/", timeout=0.2)
    assert ws.proxy is None


def test_客户端该有的方法都在() -> None:
    """这条是补的：`_parse_proxy` 当初被插在类中间，把后面几个方法吞进了它自己的
    函数体——类上于是没有 `send_text`。单测全过，真机一连就 `AttributeError`。
    所以这里直接盯着"接口在不在"，因为**它才是运行时最先用到的东西**。
    """
    ws = WebSocket("ws://127.0.0.1:1/", timeout=0.2)
    for name in ("connect", "send_text", "send_close", "recv", "close"):
        assert callable(getattr(ws, name)), f"WebSocket 少了 {name}"


def test_与本地假服务端真跑一次握手与一帧() -> None:
    """真 socket、真握手、真收一帧——`connect()` 与 `recv()` 都走一遍。"""
    import base64
    import hashlib
    import json as _json

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    seen: list[str] = []

    def serve() -> None:
        conn, _ = server.accept()
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = ""
            for line in request.decode("latin-1").splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            seen.append(key)
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            from spoolkit.bridge.ws import OP_TEXT, encode_frame

            conn.sendall(encode_frame(OP_TEXT, _json.dumps({"op": 10}).encode(), mask=False))
            # 等客户端回一帧（我们的客户端会发心跳），再收摊
            from spoolkit.bridge.ws import read_frame

            conn.settimeout(3)
            try:
                read_frame(conn)
            except Exception:
                pass
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    ws = WebSocket(f"ws://127.0.0.1:{port}/", timeout=5.0)
    ws.connect()
    assert _json.loads(ws.recv())["op"] == 10
    ws.send_text('{"op": 1, "d": null}')
    ws.close()
    assert seen  # 服务端确实收到了握手
