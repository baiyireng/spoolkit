from agents_dev.net import system_proxy


def test_优先取https代理() -> None:
    raw = {"http": "127.0.0.1:8080", "https": "127.0.0.1:7890"}
    assert system_proxy(raw) == "http://127.0.0.1:7890"


def test_没有https时退回http() -> None:
    assert system_proxy({"http": "127.0.0.1:8080"}) == "http://127.0.0.1:8080"


def test_保留已有的协议前缀() -> None:
    raw = {"https": "socks5://127.0.0.1:1080"}
    assert system_proxy(raw) == "socks5://127.0.0.1:1080"


def test_没有代理时返回空() -> None:
    assert system_proxy({}) is None

