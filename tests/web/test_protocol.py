from spoolkit.web.protocol import (
    AWAIT,
    CONFIRM,
    DELTA,
    DIFF,
    FINAL_DELTA,
    FINAL,
    NOTE,
    WAIT,
    Event,
    parse_line,
)


def test_序列化成一行JSON() -> None:
    line = Event(FINAL, {"ok": True, "text": "做完了"}).to_line()
    assert "\n" not in line
    assert '"type": "final"' in line
    assert "做完了" in line


def test_解析往返一致() -> None:
    original = Event(DIFF, {"path": "a.py", "text": "-x\n+y"})
    parsed = parse_line(original.to_line())
    assert parsed is not None
    assert parsed.type == DIFF
    assert parsed.data["path"] == "a.py"
    assert parsed.data["text"] == "-x\n+y"


def test_忽略散文行() -> None:
    """内核偶尔会打印别的（第三方库的警告），不能因为一行杂音断流。"""
    assert parse_line("step0: 工具 read_file -> 成功") is None
    assert parse_line("") is None
    assert parse_line("   ") is None


def test_忽略损坏的JSON() -> None:
    assert parse_line('{"type": "final"') is None
    assert parse_line("{不是 JSON}") is None


def test_忽略未知类型() -> None:
    assert parse_line('{"type": "凭空冒出来的"}') is None


def test_忽略非对象JSON() -> None:
    assert parse_line("[1, 2, 3]") is None
    assert parse_line('"字符串"') is None


def test_缺少type字段被忽略() -> None:
    assert parse_line('{"ok": true}') is None


def test_type不进入data() -> None:
    parsed = parse_line(Event(AWAIT, {"count": 2}).to_line())
    assert parsed is not None
    assert "type" not in parsed.data
    assert parsed.data["count"] == 2


def test_确定事件类型被接受() -> None:
    assert parse_line(Event(CONFIRM, {"applied": True}).to_line()) is not None


def test_note事件被接受() -> None:
    """系统说给用户听的一句话。未知类型会被丢掉，所以必须登记。"""
    parsed = parse_line(Event(NOTE, {"text": "数字核对：…"}).to_line())
    assert parsed is not None
    assert parsed.type == NOTE
    assert parsed.data["text"] == "数字核对：…"


def test_wait事件被接受() -> None:
    parsed = parse_line(Event(WAIT, {"elapsed": 15, "limit": 491}).to_line())
    assert parsed is not None
    assert parsed.type == WAIT
    assert parsed.data["elapsed"] == 15
    assert parsed.data["limit"] == 491
    assert "type" not in parsed.data


def test_wait可以没有limit() -> None:
    parsed = parse_line(Event(WAIT, {"elapsed": 15}).to_line())
    assert parsed is not None
    assert parsed.type == WAIT
    assert "limit" not in parsed.data


def test_final_delta事件被接受() -> None:
    """纯文本终稿增量。未登记的类型会被丢掉。"""
    parsed = parse_line(Event(FINAL_DELTA, {"text": "第一段\n第二段"}).to_line())
    assert parsed is not None
    assert parsed.type == FINAL_DELTA
    assert parsed.data["text"] == "第一段\n第二段"
    assert "\\n" not in parsed.data["text"]


def test_delta事件被接受() -> None:
    """正文流式增量：未知类型会被丢掉，必须进白名单。"""
    parsed = parse_line(
        Event(DELTA, {"text": "一段", "channel": "assistant"}).to_line()
    )
    assert parsed is not None
    assert parsed.type == DELTA
    assert parsed.data["text"] == "一段"
    assert parsed.data["channel"] == "assistant"

