from spoolkit.web.page import HTML


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


def test_页面能按快照重建待确认面板() -> None:
    """断线重连时事件已经漏掉了，只能按快照补上。"""
    assert "state.diffs" in HTML
    assert "showPending" in HTML


def test_页面按快照显示模型断开() -> None:
    """配对横幅是待批准；模型不通是另一条，不能复用「应用/拒绝」。"""
    assert "showHealth" in HTML
    assert 'id="health"' in HTML
    assert "模型连不上" in HTML
    assert "state.health" in HTML


def test_页面也报外接配方断开() -> None:
    """llama / 隧道是本机配方，不通时走同一条横幅，不另开按钮。"""
    assert "health.services" in HTML
    assert "连不上" in HTML


def test_页面按快照显示待批准的配对码() -> None:
    """给人看的一行，不进模型上下文。不能复用「应用/拒绝」——那是落盘确认。"""
    assert "pairings" in HTML
    assert "showPairings" in HTML
    assert "spool approve" in HTML
    assert 'id="pairing"' in HTML
    assert "/state" in HTML


def test_脚本里没有会被浏览器当成标签的小于号() -> None:
    """真踩过：注释写成 spool approve <码>，浏览器把 <码> 当标签，整段脚本不跑。"""
    start = HTML.find("<script>")
    end = HTML.rfind("</script>")
    assert start != -1 and end > start
    script = HTML[start + len("<script>") : end]
    assert "<" not in script
    # Python 三引号里写 '\n 会变成 JS 单引号字符串中间的真换行，整段脚本同样不跑。
    assert "\n（点" not in script


def test_页面显示模型等待心跳() -> None:
    assert "data.type === 'wait'" in HTML
    assert "模型生成中，已等待" in HTML
    assert "上限" in HTML


def test_等待心跳不占用探活横幅() -> None:
    """红色 #health 是连不上，不是还在生成。"""
    start = HTML.find("<script>")
    end = HTML.rfind("</script>")
    script = HTML[start + len("<script>") : end]
    wait_at = script.find("data.type === 'wait'")
    assert wait_at != -1
    wait_block = script[wait_at : wait_at + 280]
    assert "showHealth" not in wait_block
    assert "health" not in wait_block
