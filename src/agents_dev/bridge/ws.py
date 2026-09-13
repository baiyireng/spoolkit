"""一个够用的 WebSocket 客户端（标准库，不引依赖）。

为什么自己写：QQ 官方机器人的**事件只能从 WebSocket 网关拿**（没有轮询接口），
而这台机器上不装 OpenClaw、也不想为了一条通道拖进一整套依赖。RFC 6455 的
客户端这一侧并不复杂：握手一次 + 帧的编解码（客户端发的帧必须加掩码）。

刻意只做必需的部分，边界写在注释里：

- 支持文本/二进制/关闭/心跳（ping-pong），**分片**会拼起来但只按文本返回；
- 不做扩展协商（`permessage-deflate` 之类），也不做重连——重连属于用它的
  `qqbot.py`（它知道要 resume 到哪个 seq）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from typing import Callable
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    """握手或帧层面的失败。"""


class Timeout(Exception):
    """这一次没等到数据（**不是**连接断了）。

    网关是长连接：几十秒没有一条事件是正常的。把"超时"当成"断开"会让调用方
    每 5 秒重连一次——白费连接，还会漏掉期间的 resume。
    """


def encode_frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    """编一个帧。客户端发出的帧**必须**加掩码（RFC 6455 §5.3）。

    掩码位是第二个字节的最高位——**只把负载 XOR 了、没设这一位**，
    对端就会按"没掩码"去读，收到一堆乱字节。（这个 bug 是被
    test_qqbot 的回环服务端当场抓到的：它读到的首字节是 0x9b。）
    """
    head = bytearray([0x80 | opcode])  # FIN=1 + opcode
    length = len(payload)
    if length < 126:
        head.append(length)
    elif length < 1 << 16:
        head.append(126)
        head += struct.pack("!H", length)
    else:
        head.append(127)
        head += struct.pack("!Q", length)
    if mask:
        # 掩码位是**第二个字节**的最高位——三种长度写法都要设。
        head[1] |= 0x80
    else:
        return bytes(head) + payload
    key = os.urandom(4)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(head) + key + masked


def read_frame(sock) -> tuple[bool, int, bytes] | None:
    """读一个帧，返回（是否最后一片, 操作码, 负载）。连接关闭时返回 None。"""
    header = _read_exactly(sock, 2)
    if header is None:
        return None
    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        extra = _read_exactly(sock, 2)
        if extra is None:
            return None
        length = struct.unpack("!H", extra)[0]
    elif length == 127:
        extra = _read_exactly(sock, 8)
        if extra is None:
            return None
        length = struct.unpack("!Q", extra)[0]
    key = b""
    if masked:  # 服务端发给客户端的帧不该加掩码，但按规矩还是要认
        key = _read_exactly(sock, 4) or b""
    payload = _read_exactly(sock, length) if length else b""
    if payload is None:
        return None
    if masked and len(key) == 4:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload


def _read_exactly(sock, count: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < count:
        try:
            data = sock.recv(count - len(chunks))
        except TimeoutError:
            if chunks:
                raise WebSocketError("帧读到一半就超时了") from None
            raise Timeout() from None
        except OSError:
            return None
        if not data:
            return None
        chunks += data
    return bytes(chunks)


class WebSocket:
    """连上、收、发。用它的人负责重连与会话状态。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
        on_ping: Callable[[], None] | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._sock = None
        self._buffer: list[str] = []
        self.on_ping = on_ping

    # --- 生命周期 ---

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不支持的协议：{parsed.scheme}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            raw = self.ssl_context.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{name}: {value}" for name, value in self.headers.items()]
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = raw.recv(4096)
            if not chunk:
                raise WebSocketError("握手期间连接被关闭")
            response += chunk
        head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        status = head.splitlines()[0]
        if "101" not in status:
            raise WebSocketError(f"握手失败：{status}")
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()
        ).decode()
        if f"sec-websocket-accept: {expected}".lower() not in head.lower():
            raise WebSocketError("握手回来的 Sec-WebSocket-Accept 对不上")
        self._sock = raw

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_close(self, code: int = 1000) -> None:
        self._send(OP_CLOSE, struct.pack("!H", code))

    def recv(self) -> str | None:
        """收一条**文本**消息。对端关闭时返回 None。

        分片按 FIN 拼起来；心跳在这里就地回答（不回它，对端会认为我们掉线）。
        """
        buffer = bytearray()
        while True:
            frame = read_frame(self._sock)
            if frame is None:
                return None
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                if self.on_ping is not None:
                    self.on_ping()
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                return None
            buffer += payload
            if fin:
                return buffer.decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self.send_close()
        except OSError:
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None

    def _send(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("连接没起来")
        self._sock.sendall(encode_frame(opcode, payload))
