"""WebSocket 客户端自身的边界。

`test_socks.py` 里那条"真跑一次握手与一帧"是**偶发**失败：单跑必过、全量跑
才炸。偶发不是运气问题，是竞态——把它固定成确定性用例，才修得住。
"""

import base64
import hashlib
import json
import socket
import threading
import time

import pytest

from spoolkit.bridge.ws import OP_TEXT, Timeout, WebSocket, encode_frame

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _serve(server: socket.socket, frames: list[bytes], together: bool) -> None:
    """假服务端：回握手，再发 `frames` 里的帧。

    `together=True` 时把握手响应**和第一帧写进同一次 sendall**——真机上一个
    TCP 段里塞下这两样完全正常，而读握手的那次 `recv(4096)` 会把它们一起读回来。
    """
    conn, _ = server.accept()
    try:
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = ""
        for line in request.decode("latin-1").splitlines():
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
        if together:
            conn.sendall(handshake + b"".join(frames))
        else:
            conn.sendall(handshake)
            for frame in frames:
                conn.sendall(frame)
        # 客户端会回心跳：等它一帧，免得我们这边先关把客户端的 send 打断。
        conn.settimeout(3)
        try:
            conn.recv(4096)
        except OSError:
            pass
    finally:
        conn.close()


def _start(frames: list[bytes], together: bool) -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=_serve, args=(server, frames, together), daemon=True).start()
    return port


def _text_frame(payload: dict) -> bytes:
    return encode_frame(OP_TEXT, json.dumps(payload).encode(), mask=False)


def test_握手响应与第一帧同段到达时不丢帧() -> None:
    """握手多读出来的字节必须留着，不能丢。

    踩过的坑：`connect()` 只取 `\\r\\n\\r\\n` 前面的头部，后面跟着的帧字节被
    悄悄扔掉——`recv()` 于是去等一个**已经到了**的帧，直到对端关闭才返回 None。
    症状是"偶发连不上"，全量跑测试时机器更忙、更容易合并成一段，就必现。
    """
    port = _start([_text_frame({"op": 10, "d": {"heartbeat_interval": 41250}})], True)
    ws = WebSocket(f"ws://127.0.0.1:{port}/", timeout=5.0)
    ws.connect()
    text = ws.recv()
    ws.close()
    assert text is not None, "握手后的第一帧被丢掉了"
    assert json.loads(text)["op"] == 10


def test_同段到达的后续帧按顺序读出来() -> None:
    """一段里塞两帧时，第二帧也不能丢（留的字节要能被下一次 recv 用上）。"""
    port = _start(
        [_text_frame({"op": 10}), _text_frame({"op": 0, "t": "C2C_MESSAGE_CREATE"})],
        True,
    )
    ws = WebSocket(f"ws://127.0.0.1:{port}/", timeout=5.0)
    ws.connect()
    first = ws.recv()
    second = ws.recv()
    ws.close()
    assert json.loads(first)["op"] == 10
    assert json.loads(second)["t"] == "C2C_MESSAGE_CREATE"


def test_单次超时只影响这一次调用() -> None:
    """`recv(timeout=...)` 是"这次最多等这么久"，不是把连接的设置改掉。

    用它的地方正是心跳：等待长度要服从下一次心跳的时刻。若它把 socket 上的
    超时永久改小，后续读就会变得很脆；永久改大，心跳又会迟到。
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

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
            accept = base64.b64encode(
                hashlib.sha1((key + GUID).encode()).digest()
            ).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            time.sleep(0.6)  # 先沉默一会儿，让客户端的短超时先到
            conn.sendall(_text_frame({"op": 10}))
            conn.settimeout(3)
            try:
                conn.recv(4096)
            except OSError:
                pass
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    ws = WebSocket(f"ws://127.0.0.1:{port}/", timeout=5.0)
    ws.connect()
    started = time.monotonic()
    with pytest.raises(Timeout):
        ws.recv(timeout=0.2)
    assert time.monotonic() - started < 1.0, "单次超时没生效"
    # 之后再读还是按建连接时的 5 秒来等，能正常收到那一帧
    assert json.loads(ws.recv())["op"] == 10
    ws.close()
