import pytest

from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.llm.types import ChatRequest, Message


def _req(text: str) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content=text),),
        max_tokens=256,
    )


def test_按脚本顺序返回响应() -> None:
    model = FakeModel(script=["第一步", "第二步"], tokenizer=OfflineTokenCounter())
    assert model.chat(_req("a")).text == "第一步"
    assert model.chat(_req("b")).text == "第二步"


def test_脚本耗尽后抛错() -> None:
    model = FakeModel(script=["只有一条"], tokenizer=OfflineTokenCounter())
    model.chat(_req("a"))
    with pytest.raises(RuntimeError):
        model.chat(_req("b"))


def test_记录收到的请求便于断言() -> None:
    model = FakeModel(script=["ok"], tokenizer=OfflineTokenCounter())
    model.chat(_req("请读文件"))
    assert len(model.requests) == 1
    assert model.requests[0].messages[0].content == "请读文件"


def test_统计token数() -> None:
    model = FakeModel(script=["返回内容"], tokenizer=OfflineTokenCounter())
    resp = model.chat(_req("提示"))
    assert resp.prompt_tokens > 0
    assert resp.completion_tokens > 0


def test_剩余条数可查() -> None:
    model = FakeModel(script=["a", "b"], tokenizer=OfflineTokenCounter())
    assert model.remaining == 2
    model.chat(_req("x"))
    assert model.remaining == 1

