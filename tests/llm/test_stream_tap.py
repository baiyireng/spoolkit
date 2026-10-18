from spoolkit.llm.stream_tap import FinalFieldTap


def test_final字段出现前不吐() -> None:
    tap = FinalFieldTap()
    assert tap.feed('{"thought": "x", ') == ""
    assert tap.feed('"tool_calls": []') == ""


def test_final字段增量吐出() -> None:
    tap = FinalFieldTap()
    assert tap.feed('{"final": "你好') == "你好"
    assert tap.feed("世界") == "世界"
    assert tap.feed('"}') == ""


def test_分片跨过键名() -> None:
    tap = FinalFieldTap()
    assert tap.feed('{"fin') == ""
    assert tap.feed('al": "ab') == "ab"
    assert tap.feed('c"}') == "c"


def test_json转义解码为真实换行() -> None:
    """schema 流里 final 是 JSON 字符串；delta 必须是解码后正文，不能留字面 \\n。"""
    tap = FinalFieldTap()
    # 缓冲里是 JSON 原文：反斜杠 + n
    assert tap.feed('{"final": "甲\\n乙"}') == "甲\n乙"


def test_转义跨分片() -> None:
    tap = FinalFieldTap()
    # 前半片可先吐出普通字符；半截 `\` 留到下一片再解码。
    assert tap.feed('{"final": "x\\') == "x"
    assert tap.feed('ny"}') == "\ny"


def test_引号与反斜杠转义() -> None:
    tap = FinalFieldTap()
    assert tap.feed('{"final": "说\\"嗨\\\\"') == '说"嗨\\'
    assert tap.feed('"}') == ""
