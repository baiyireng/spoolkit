from spoolkit.cli.app import DEFAULT_WINDOW, resolve_window
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter


def _gateway(window: int | None = None) -> FakeModel:
    return FakeModel(script=[], tokenizer=OfflineTokenCounter(), window=window)


def test_显式指定时优先使用() -> None:
    assert resolve_window(_gateway(8192), 2048) == 2048


def test_未指定时向供应商查询() -> None:
    assert resolve_window(_gateway(4096), 0) == 4096


def test_查询不到时退回默认值() -> None:
    assert resolve_window(_gateway(None), 0) == DEFAULT_WINDOW


def test_大窗口不再被我们砍掉() -> None:
    """这里原先写死过一个 32768 的上限，理由是「不封顶会让短上下文特化失去意义」
    ——那是为了验证我们自己的设计，不是为了让活干得更好。

    代价很实在：远程供应商报 200K，我们按 32K 跑，能力被自己砍掉五分之四。
    判据：**这个数字只会因为模型更强而被突破吗？** 是 → 默认不该压低它。
    """
    assert resolve_window(_gateway(1048576), 0) == 1048576


def test_普通窗口原样采用() -> None:
    assert resolve_window(_gateway(16384), 0) == 16384


def test_想限制就显式给window() -> None:
    assert resolve_window(_gateway(1048576), 65536) == 65536
