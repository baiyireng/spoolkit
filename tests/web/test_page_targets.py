"""页面顶上的目标选择栏：工作区 / 会话两条下拉 + 当前目标全路径。

这套壳**不许引浏览器、也不许引前端框架**（设计 §4 的 non-goal：单文件分发、零新依赖），
所以这里量的是**页面的结构**：HTML/JS 文本里有没有那几条渲染分支、切换走的是不是
`POST /target`、有没有"为了切换去重连事件流"这种写法。

每条断言都要**能证伪**：把实现改回旧行为（页面根本没有选择栏、目标变了只更新横幅、
切换时重连 `EventSource`）它必须变红。所以断言钉在**分支**上，不是"某个词出现过"。
"""

from spoolkit.web.page import HTML


def _script() -> str:
    start = HTML.find("<script>")
    end = HTML.rfind("</script>")
    assert start != -1 and end > start, "页面里没有脚本段"
    return HTML[start + len("<script>") : end]


def _body(name: str) -> str:
    """取某个 JS 函数的大括号之间那一段（`function name(` 起，花括号配平）。

    配平而不是正则：要看的分支大多嵌在循环与回调里，正则取不出嵌套的 `{}`。
    对象字面量的括号是配平的，所以计数仍然准。
    """
    script = _script()
    head = script.find("function " + name + "(")
    assert head != -1, f"页面里没有 {name}()"
    opened = script.find("{", head)
    depth = 0
    for index in range(opened, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[opened : index + 1]
    raise AssertionError(f"{name}() 的花括号不配平")


# --- 选择栏本身 ---


def test_顶部有工作区与会话两条选择栏() -> None:
    assert "<select" not in HTML
    assert 'id="ws"' not in HTML
    assert 'id="sess"' not in HTML
    assert "setAttribute('role', 'button')" in _body("row")
    body = _body("drawTarget")
    # 两条下拉的备选就是 /state 里给它们准备的那两个字段
    assert "state.workspaces" in body
    assert "state.sessions" in body
    # 当前会话**不一定**在会话表里（`cli` 这种没跑过的名字不出现）：以 target 为准
    assert "t.session" in body


def test_当前目标的全路径摆在页面上() -> None:
    """顶栏只显示工作区名和会话标题。完整根路径在 title 上，悬停能看到。"""
    assert 'id="where"' in HTML

    body = _body("drawTarget")

    assert "whereBox.textContent = wsName + ' · ' + sessName" in body
    assert "whereBox.title = t.root" in body
    assert "还没有目标" in body
    assert "whereBox.textContent = t.root" not in body


# --- 切换：POST /target，且不重连事件流 ---


def test_切换走的是POST_target() -> None:
    body = _body("switchTarget")

    assert "post('/target'" in body
    assert "applyState(next)" in body, "切换成功后必须按返回体重绑"


def test_两条下拉各自发起切换() -> None:
    body = _body("target")

    assert "ctx.switchTarget({workspace: workspace})" in body
    assert "ctx.switchTarget({session: name})" in body


def test_切换不重连事件流() -> None:
    """目标变了服务端会退订旧的、订阅新的、补一帧 state（设计 §3.3）。

    页面自己重连＝把这件事做重了，而且重连那一瞬间的事件必然漏掉。
    """
    script = _script()

    assert script.count("new EventSource(") == 1
    assert "EventSource" in _body("connect")
    assert "EventSource" not in _body("switchTarget")


def test_state帧会重画目标与历史() -> None:
    """切换后服务端补的那一帧 `state` 也要**整屏重绑**，不能只更新状态区。"""
    body = _body("onEvent")

    assert "applyState(data)" in body
    apply_body = _body("applyState")
    assert "drawTarget(state)" in apply_body
    assert "resetView()" in apply_body
    assert "drawHistory(state.messages)" in apply_body

    reset = _body("resetView")
    assert "stream.replaceChildren()" in reset, "换了目标还留着上一个目标的输出"
    assert "ctx.store.set('drawn', 0)" in reset, "不给新目标重画历史的门"


# --- 三种状态各有分支 ---


def test_空态引导人选工作区() -> None:
    """顶部那条常驻横幅没了：空态引导改由输入条那行的「未选工作区」徽标承载
    （点开是同一句话）。判据一个字都没丢，只是不再占顶部的位置。

    钉**要点**：空态说得出"没选工作区"这件事，也给出下一步（去哪一段选、
    表空的怎么造出候选），并且那句话仍以服务端为准、页面只兜底。
    """
    assert 'id="st-target"' in HTML
    assert 'id="target-note"' not in HTML, "顶部那条常驻横幅还在"

    body = _body("drawTarget")

    assert "if (!t.root)" in body
    assert "未选择工作区" in body
    assert "登记表为空" in body, "登记表为空时没给出路（只让人对着空列表选）"
    assert "t.note" in body, "空态那句话以服务端为准，页面只兜底"
    assert "ctx.store.set('targetMissing', !t.root)" in body, "空态那枚徽标没有画出来"
    assert "drawStatus()" in body, "空态那句话没有落到状态区"


def test_pinned时选择栏禁用并说明原因() -> None:
    """`--root` 起的壳只有一个目标：灰掉，并把原因写出来（不然人对着灰按钮发呆）。"""
    body = _body("drawTarget")

    assert "t.pinned" in body
    assert "lockRows(workspaceList, locked)" in _body("renderSidebar")
    assert "line.tabIndex = locked ? -1 : 0" in _body("lockRows")
    assert "--root" in body


def test_别处还有一条在跑() -> None:
    """切走不杀任务（设计 §3.4），页面就必须说得出这件事，别让它看起来只有一条。

    钉**要点**：说的是"其他目标仍在运行"、**是哪一个**目标、以及它在等确认；
    措辞可以换（"别处还有一条在跑"→"其他目标仍在运行"），这三件事实不行。
    """
    body = _body("showElsewhere")

    assert "state.running_targets" in body
    assert "!row.current" in body
    assert "其他会话运行中" in body, "读不出其他会话仍在运行"
    assert "row.name" in body, "没说是哪个目标在跑"
    assert "awaiting" in body and "等待确认" in body, "它在等确认这件事丢了"
    assert "short(row.goal, 60)" in body, "目标要截断，一条长 goal 会把这一行撑爆"

    # 别的目标上的任务，在本目标的事件流上**一个字都不会出现**：只能轮 /state
    assert "showElsewhere(state)" in _body("refreshStatus")


def test_请求被拒时把error说给人看() -> None:
    """本地单机壳没人开着 devtools："点了一下什么都没发生"是最坏的那种失败。"""
    assert "console.log" not in _script()

    body = _body("post")

    assert "detail.error" in body
    assert "add(" in body

    # 切换被拒（409/400）同样要落在页面上，而且选择栏要拨回当前目标
    switch = _body("switchTarget")
    assert "next === null" in switch
    assert "drawTarget(store.get('lastState'))" in switch
