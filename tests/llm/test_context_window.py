import httpx

from spoolkit.llm.fake import FakeModel
from spoolkit.llm.gemini import GeminiGateway
from spoolkit.llm.llamacpp import LlamaCppGateway
from spoolkit.llm.tokenizer import OfflineTokenCounter


def _llama(handler) -> LlamaCppGateway:
    return LlamaCppGateway(transport=httpx.MockTransport(handler))


def test_llamacpp从props读取窗口() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/props"
        return httpx.Response(
            200, json={"default_generation_settings": {"n_ctx": 8192}}
        )

    gateway = _llama(handler)
    assert gateway.context_window() == 8192
    gateway.close()


def test_llamacpp兼容顶层n_ctx() -> None:
    gateway = _llama(lambda request: httpx.Response(200, json={"n_ctx": 4096}))
    assert gateway.context_window() == 4096
    gateway.close()


def test_llamacpp查不到时返回空() -> None:
    gateway = _llama(lambda request: httpx.Response(200, json={}))
    assert gateway.context_window() is None
    gateway.close()


def test_llamacpp服务不可达时返回空而不是抛错() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    gateway = _llama(handler)
    assert gateway.context_window() is None
    gateway.close()


def test_llamacpp接口异常时返回空() -> None:
    gateway = _llama(lambda request: httpx.Response(500, text="boom"))
    assert gateway.context_window() is None
    gateway.close()


def test_gemini从模型信息读取上限() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("models/gemini-3.6-flash")
        return httpx.Response(200, json={"inputTokenLimit": 1048576})

    gateway = GeminiGateway("k", transport=httpx.MockTransport(handler))
    assert gateway.context_window() == 1048576
    gateway.close()


def test_gemini缺少字段时返回空() -> None:
    gateway = GeminiGateway(
        "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    assert gateway.context_window() is None
    gateway.close()


def test_假模型可配置窗口便于测试() -> None:
    model = FakeModel(
        script=[], tokenizer=OfflineTokenCounter(), window=2048
    )
    assert model.context_window() == 2048


def test_假模型默认不报告窗口() -> None:
    assert FakeModel(script=[], tokenizer=OfflineTokenCounter()).context_window() is None

