import io
import json

from agents_dev.cli.events import EventWriter
from agents_dev.web.protocol import FINAL, parse_line


def test_输出一行JSON() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).emit(FINAL, ok=True, text="好了")
    line = buffer.getvalue().strip()
    assert json.loads(line)["type"] == FINAL


def test_可解析回来() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).emit(FINAL, ok=True, text="好了")
    parsed = parse_line(buffer.getvalue())
    assert parsed is not None
    assert parsed.data["text"] == "好了"


def test_每次输出都冲洗() -> None:
    """不 flush 的话输出会攒在缓冲区里，UI 看起来像卡住。"""

    class _Probe(io.StringIO):
        flushed = 0

        def flush(self) -> None:
            type(self).flushed += 1

    probe = _Probe()
    EventWriter(probe).emit(FINAL, ok=True, text="x")
    assert probe.flushed >= 1


def test_连续输出是多行() -> None:
    buffer = io.StringIO()
    writer = EventWriter(buffer)
    writer.emit(FINAL, ok=True, text="一")
    writer.emit(FINAL, ok=False, text="二")
    assert len(buffer.getvalue().strip().splitlines()) == 2


def test_handle可直接作为回调() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).handle("step", {"n": 3})
    assert json.loads(buffer.getvalue().strip())["n"] == 3

