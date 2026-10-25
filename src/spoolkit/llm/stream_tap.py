"""从流式 JSON 正文里抠出 `final` 字段的增长量。

主循环用 schema 约束，模型吐的是整段 JSON。真正给人看的结论在 `final`
字符串里——流式时先看到 thought / tool_calls，不能整段当 assistant 上屏。
本抽取器只在 `"final"` 字符串值增长时回调，半截 JSON 也不炸。

吐出的是**解码后的**可见文本（`\\n` → 换行），与最终解析出的 `final` 同形，
CLI Live / Web 气泡才能按 Markdown 正常换行。
"""

from __future__ import annotations

_SIMPLE_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
}


class FinalFieldTap:
    """喂入流式 chunk，返回应作为 assistant delta 发出的新增后缀。"""

    def __init__(self) -> None:
        self._buf = ""
        self._emitted = 0
        self._in_final = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        if not self._in_final:
            marker = self._find_final_open()
            if marker < 0:
                return ""
            self._in_final = True
            self._emitted = 0
            # 从开引号之后开始算内容
            self._buf = self._buf[marker:]
        return self._drain_final_value()

    def _find_final_open(self) -> int:
        """返回 `final` 值开引号之后的下标；尚未出现则 -1。"""
        keys = ('"final"', '"Final"')
        lower = self._buf
        for key in keys:
            at = lower.find(key)
            if at < 0:
                continue
            rest = lower[at + len(key) :]
            i = 0
            while i < len(rest) and rest[i] in " \t\n\r":
                i += 1
            if i >= len(rest) or rest[i] != ":":
                continue
            i += 1
            while i < len(rest) and rest[i] in " \t\n\r":
                i += 1
            if i < len(rest) and rest[i] == '"':
                return at + len(key) + i + 1
        return -1

    def _drain_final_value(self) -> str:
        """已在 final 字符串内：解码 JSON 转义，吐出相对上次的新增可见字符。

        半截转义（末尾单独的 `\\` 或未写完的 `\\uXXXX`）留在缓冲里等下一片。
        """
        out_chars: list[str] = []
        i = self._emitted
        buf = self._buf
        while i < len(buf):
            ch = buf[i]
            if ch == '"':
                self._emitted = i
                return "".join(out_chars)
            if ch == "\\":
                decoded, consumed = _try_decode_escape(buf, i)
                if consumed == 0:
                    break  # 转义还不完整
                out_chars.append(decoded)
                i += consumed
                continue
            out_chars.append(ch)
            i += 1
        self._emitted = i
        return "".join(out_chars)


def _try_decode_escape(buf: str, at: int) -> tuple[str, int]:
    """从 `buf[at]`（必为 `\\`）解码一个 JSON 转义。

    返回 `(解码字符, 消耗长度)`；数据不够时返回 `("", 0)`。
    """
    if at + 1 >= len(buf):
        return "", 0
    nxt = buf[at + 1]
    if nxt == "u":
        if at + 5 >= len(buf):
            return "", 0
        hexpart = buf[at + 2 : at + 6]
        if len(hexpart) < 4 or any(
            c not in "0123456789abcdefABCDEF" for c in hexpart
        ):
            # 非法 \\u：把反斜杠当字面吐出，让后续字符自行处理
            return "\\", 1
        return chr(int(hexpart, 16)), 6
    if nxt in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[nxt], 2
    # 未知转义：按 JSON 宽松习惯吐出转义符本身
    return nxt, 2
