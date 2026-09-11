from agents_dev.cli.app import (
    DEFAULT_WINDOW,
    MAX_AUTO_WINDOW,
    resolve_window,
)
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter


def _gateway(window: int | None = None) -> FakeModel:
    return FakeModel(script=[], tokenizer=OfflineTokenCounter(), window=window)


def test_显式指定时优先使用() -> None:
    assert resolve_window(_gateway(8192), 2048) == 2048


def test_未指定时向供应商查询() -> None:
    assert resolve_window(_gateway(4096), 0) == 4096


def test_查询不到时退回默认值() -> None:
    assert resolve_window(_gateway(None), 0) == DEFAULT_WINDOW


def test_超大窗口被压到上限() -> None:
    assert resolve_window(_gateway(1048576), 0) == MAX_AUTO_WINDOW


def test_上限以内的窗口原样采用() -> None:
    assert resolve_window(_gateway(16384), 0) == 16384


def test_显式指定不受上限约束() -> None:
    assert resolve_window(_gateway(1048576), 65536) == 65536

