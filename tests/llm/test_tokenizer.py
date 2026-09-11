from agents_dev.llm.tokenizer import OfflineTokenCounter


def test_空字符串为零() -> None:
    assert OfflineTokenCounter().count("") == 0


def test_计数随长度单调增长() -> None:
    counter = OfflineTokenCounter()
    assert counter.count("a" * 100) > counter.count("a" * 10)


def test_中文比等长英文消耗更多() -> None:
    counter = OfflineTokenCounter()
    assert counter.count("中" * 50) > counter.count("z" * 50)


def test_估算不低估量级() -> None:
    counter = OfflineTokenCounter()
    assert counter.count("hello world " * 20) >= 40


def test_同一输入结果稳定() -> None:
    counter = OfflineTokenCounter()
    text = "def f():\n    return 1\n"
    assert counter.count(text) == counter.count(text)

