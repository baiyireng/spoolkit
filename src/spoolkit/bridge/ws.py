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


def socks5_connect(
    proxy_host: str, proxy_port: int, host: str, port: int, timeout: float = 15.0
) -> socket.socket:
    """经 SOCKS5 连到目标，返回已经打通的 socket。

    为什么需要它：QQ 官方机器人的平台侧有 **IP 白名单**（`code=11298`），
    而家里那条宽带的出口 IP 会变。借一台云主机出去（`ssh -D` 给的 SOCKS5），
    腾讯看到的就是那台主机的固定 IP——白名单只需要填一次。

    这里不用第三方库：SOCKS5 的无认证握手只有几行，而为了它在主包里拖一个
    依赖不划算（httpx 那边要 SOCKS 才需要 `httpx[socks]`，那是可选加装）。
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if sock.recv(2) != b"\x05\x00":
            raise WebSocketError("代理不接受无认证的 SOCKS5")
        try:
            packed = socket.inet_aton(host)
            request = b"\x05\x01\x00\x01" + packed + port.to_bytes(2, "big")
        except OSError:
            name = host.encode()
            request = (
                b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big")
            )
        sock.sendall(request)
        head = sock.recv(4)
        if len(head) < 4 or head[1] != 0:
            raise WebSocketError(f"SOCKS5 拒绝连接：码 {head[1] if len(head) > 1 else '?'}")
        # 把 BND.ADDR/BND.PORT 读掉，否则会混进后面的数据流。
        kind = head[3]
        extra = 4 if kind == 1 else 16 if kind == 4 else 0
        if extra:
            sock.recv(extra + 2)
        return sock
    except BaseException:
        sock.close()
        raise


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


class _Buffered:
    """先吐出手握时**多读**到的字节，再落到真 socket 上。

    为什么需要：服务端可能把握手响应和第一个数据帧写进同一个 TCP 段，`connect()`
    里那次 `recv(4096)` 会把两段一起读回来。当初只取了 `\\r\\n\\r\\n` 前面的头部，
    跟在后面的帧字节被悄悄扔掉——`recv()` 于是去等一个**已经到了**的帧，一直等到
    对端关闭才返回 None。症状是"偶发连不上"：单跑测试必过，全量跑（机器更忙、
    更容易合并成一段）才炸。
    """

    def __init__(self, sock, pending: bytes = b"") -> None:
        self._sock = sock
        self._pending = bytearray(pending)

    def recv(self, count: int) -> bytes:
        if self._pending:
            chunk = bytes(self._pending[:count])
            del self._pending[:count]
            return chunk
        return self._sock.recv(count)

    def sendall(self, data) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()

    def __getattr__(self, name: str):
        # 其余方法（settimeout 之类）直接借真 socket 的。
        return getattr(self._sock, name)


class WebSocket:
    """连上、收、发。用它的人负责重连与会话状态。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
        on_ping: Callable[[], None] | None = None,
        proxy: str | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._sock = None
        self._buffer: list[str] = []
        self.on_ping = on_ping
        # 对端发关闭帧时记下它的码（排错时"为什么断的"全靠它）。
        self.close_info: str = ""
        # 形如 "socks5://127.0.0.1:1080"（或 "127.0.0.1:1080"）。
        self.proxy = proxy

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

        if self.proxy:
            proxy_host, proxy_port = _parse_proxy(self.proxy)
            raw = socks5_connect(proxy_host, proxy_port, host, port, self.timeout)
        else:
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
        head_bytes, _, leftover = response.partition(b"\r\n\r\n")
        head = head_bytes.decode("latin-1")
        status = head.splitlines()[0]
        if "101" not in status:
            raise WebSocketError(f"握手失败：{status}")
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()
        ).decode()
        if f"sec-websocket-accept: {expected}".lower() not in head.lower():
            raise WebSocketError("握手回来的 Sec-WebSocket-Accept 对不上")
        # 握手多读到的字节**必须**留着（见 `_Buffered` 的注释）。
        self._sock = _Buffered(raw, leftover)


    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_close(self, code: int = 1000) -> None:
        self._send(OP_CLOSE, struct.pack("!H", code))

    def recv(self, timeout: float | None = None) -> str | None:
        """收一条**文本**消息。对端关闭时返回 None。

        `timeout` 只对这一次调用生效（不传就用建连接时的）。为什么需要它：
        用的人往往有"过一会儿必须做点别的事"的需求（比如按时发心跳），而单靠
        建连接时的那个固定超时，这件事做不准——真机上就是这么被平台掐线的。
        """
        if timeout is None:
            return self._recv_text()
        sock = self._sock
        restore = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            return self._recv_text()
        finally:
            if sock is self._sock:
                try:
                    sock.settimeout(restore)
                except OSError:
                    pass

    def _recv_text(self) -> str | None:
        """分片按 FIN 拼起来；心跳在这里就地回答（不回它，对端会认为我们掉线）。"""
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
                self.close_info = _close_text(payload)
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


def _close_text(payload: bytes) -> str:
    """把关闭帧的负载翻成人话：前两字节是码，其余是原因。"""
    if len(payload) < 2:
        return "对端没给关闭码"
    code = struct.unpack("!H", payload[:2])[0]
    reason = payload[2:].decode("utf-8", errors="replace")
    return f"code={code}" + (f" {reason}" if reason else "")


def _parse_proxy(proxy: str) -> tuple[str, int]:
    """把 `socks5://host:port` 拆开。写在文件末尾是因为它当初插在类中间，
    把后面几个方法**吞进了它自己的函数体**（缩进还在，于是它们成了
    `_parse_proxy` 里永远执行不到的内部函数）——症状是 `WebSocket` 上
    没有 `send_text`，而单测看不出来，真机一跑就炸。
    """
    text = proxy.strip()
    for prefix in ("socks5h://", "socks5://", "socks://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    host, _, port = text.partition(":")
    if not host or not port:
        raise WebSocketError(f"代理地址看不懂：{proxy}（要写成 socks5://host:port）")
    return host, int(port)
