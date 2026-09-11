from agents_dev.web.page import HTML


def test_页面是一个完整的HTML文档() -> None:
    assert HTML.lstrip().lower().startswith("<!doctype")
    assert "</html>" in HTML.lower()


def test_页面连接到事件流() -> None:
    assert "/events" in HTML
    assert "EventSource" in HTML


def test_页面能发起运行与确认() -> None:
    assert "/run" in HTML
    assert "/confirm" in HTML


def test_页面有待确认面板() -> None:
    assert "pending" in HTML
    assert "应用" in HTML
    assert "拒绝" in HTML


def test_页面在事件流断开时提示() -> None:
    """连不上却不说话，你会以为任务没跑。"""
    assert "onerror" in HTML
    assert "断开" in HTML


def test_页面没有外链资源() -> None:
    """单文件分发：不引 CDN、不引构建产物。"""
    assert "<link" not in HTML.lower()
    assert "src=" not in HTML.lower()
    assert "cdn" not in HTML.lower()


def test_确认按钮点击后会禁用() -> None:
    """重复写 stdin 会让后续的 input() 拿到意外的输入。"""
    assert "decided" in HTML
    assert "disabled = true" in HTML

