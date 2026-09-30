"""页面的结构层断言：没有浏览器驱动，只能量 HTML/JS 文本里的**分支**。

⚠️ 这一层**证伪不了视觉**（颜色、间距、圆角、响应式断点这些只能人眼看，见交付
说明里"哪些没能真在浏览器里看过"）。所以每条断言都钉在一件**去掉就坏**的事上：
某个容器没了、某条分支没了、某个类名改了、某个处理函数没被绑上。

唯一能看见"脚本真的是脚本"的地方是 `test_内联脚本能过node语法检查`：
它把内联 JS 抽出来交给 `node --check`。文本层量不出来的东西（少一个括号、
少一个引号、`\\n` 写成真换行）在那里会红。
"""

import shutil
import subprocess

import pytest

from spoolkit.web.page import HTML


def _script() -> str:
    start = HTML.find("<script>")
    end = HTML.rfind("</script>")
    assert start != -1 and end > start, "页面里没有脚本段"
    return HTML[start + len("<script>") : end]


def _find(name: str) -> int:
    at = _script().find(name)
    assert at != -1, f"脚本里没有 {name}"
    return at


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
    """单文件分发：不引 CDN、不引构建产物、断网也能用。"""
    assert "<link" not in HTML.lower()
    assert "src=" not in HTML.lower()
    assert "cdn" not in HTML.lower()
    # 图标是内联 SVG、字体是系统字体栈：一个 http(s) 地址都不该有
    # （有的话它在断网时就是一个转圈的空白）。
    assert "http://" not in HTML
    assert "https://" not in HTML


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


def test_脚本里没有能把脚本提前收掉的序列() -> None:
    """真踩过：注释写成 `spool approve <码>`，浏览器把 `<码>` 当标签，整段脚本不跑。

    这条原先写的是"脚本里一个 `<` 都不许有"——**收紧过头了**，而紧过头的检查会
    挡住本该做的事：内联 SVG 图标（零外链图标那一条要求）天生带 `<svg ...>`。
    真正的危险只有两样，而且它们都不出现在**别的**地方：

    - `</` 紧跟 `script`：HTML 解析器在这里就把脚本块收掉，后面的是纯文本；
    - 真换行漏进字符串字面量（Python 三引号里写 `'...\\n...'` 少一个反斜杠）：
      JS 字符串在行尾断掉，整段脚本报语法错、一个字都不跑。这一条靠
      "JS 字符串不许跨行"配合 —— 三引号里写的是 `\\\\n`，落进页面才是 `\\n`。

    证伪法：把某个 SVG 图标串改成 `<\\/script>`，或把一句 `'...\\\\n...'` 改成真换行，
    这里就红（后者 node --check 也会红）。
    """
    script = _script()
    assert "</script" not in script.lower(), "脚本里出现了能提前收掉脚本块的序列"
    assert "<!--" not in script, "脚本里出现了 HTML 注释起始符"
    # Python 三引号里写 '\n 会变成 JS 单引号字符串中间的真换行，整段脚本同样不跑。
    assert "\n（点" not in script
    # 反证：内联图标确实在（否则上面两条会"因为页面啥都没有"而假绿）。
    assert "<svg" in script and "viewBox" in script


def test_页面显示模型等待心跳() -> None:
    assert "data.type === 'wait'" in HTML
    assert "模型生成中，已等待" in HTML
    assert "上限" in HTML


def test_等待心跳不占用探活横幅() -> None:
    """红色 #health 是连不上，不是还在生成。"""
    script = _script()
    wait_at = script.find("data.type === 'wait'")
    assert wait_at != -1
    wait_block = script[wait_at : wait_at + 280]
    assert "showHealth" not in wait_block
    assert "health" not in wait_block


# --- 内联脚本的语法（唯一"真跑得起来"的机械证据） ---


def test_内联脚本能过node语法检查(tmp_path) -> None:
    """把内联 JS 抽成 `.js` 交给 `node --check`。

    为什么值得单独一条：结构层断言量的是"有没有那个词"，而语法错（少一个括号、
    漏一个引号、`\\n` 写成了真换行）会让整段脚本**一个字都不跑**——那时页面上
    所有的结构断言照样全绿，而人打开是一片空白。`node --check` 是这里唯一
    能证伪那件事的东西。

    为什么抽成 `.js` 而不是喂 `.html`：本仓库的 dogfood 真踩过——
    `node --check x.html` 报 `ERR_UNKNOWN_FILE_EXTENSION`（它按扩展名判类型）。

    node 不在 PATH 就跳过：这台机器没有，也不该让用例红（仓库的
    `spoolkit/tools/verify.py::make_js_verifier` 同一个态度）。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不在 PATH")
    path = tmp_path / "inline.js"
    path.write_text(_script(), encoding="utf-8")
    done = subprocess.run(
        [node, "--check", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # 反过来钉住抽取本身：抽错了（比如抽成空串）node 也会"通过"。
    assert len(_script()) > 4000
    assert done.returncode == 0, done.stderr or done.stdout


# --- 左栏：工作区 → 它会话 ---


def test_左栏是两段式的会话列表() -> None:
    """ChatGPT 那套：左边一条会话栏，按工作区分段，段里是它的会话。"""
    assert 'id="sidebar"' in HTML
    assert 'id="workspaces"' in HTML
    assert 'id="sessions"' in HTML

    body = _body("renderSidebar")
    # 两段各自的名字，以及"每一段都要有列表"这件事
    assert "工作区" in body
    assert "会话" in body
    assert "workspaceList.appendChild" in body
    assert "sessionList.appendChild" in body
    # 当前目标要能一眼看出来（高亮）
    assert "classList.add('on')" in _body("row")
    assert "sessionList.appendChild(line)" in body


def test_左栏的行点下去就是切目标() -> None:
    """行是 div，键盘与读屏够不着：真正的控件是两条原生 select，行负责点它们。

    所以切换**只有一条代码路径**（原生 change → switchTarget），
    而且"点一行"必须真的落到那个控件上，不能自己另发一次请求。
    """
    sidebar = _body("renderSidebar")
    assert "target(" in sidebar, "行没有落到原生控件上"
    assert "switchTarget" not in sidebar, "行自己发了一次请求：切换就有了第二条路径"

    dispatch = _body("target")
    assert "wsBox" in dispatch
    assert "sessBox" in dispatch
    assert "dispatchEvent" in dispatch
    # 目标名不一定已经是控件里的一项（当前会话没跑过就不在会话表里）：
    # 不先把那项补出来，赋 value 会被浏览器丢掉，点一行就变成"不改会话"。
    assert "pick(" in dispatch
    assert "box.value = value" in _body("pick")

    script = _script()
    assert "wsBox.addEventListener('change'" in script
    assert "sessBox.addEventListener('change'" in script


def test_左栏标出哪个目标在跑() -> None:
    """切走不杀任务（设计 §3.4）：正在跑的目标必须标出来。

    判据只有 `state.running_targets` 一处（`running` 那个布尔值只讲当前目标）。
    """
    body = _body("renderSidebar")
    assert "markRunning(" in body
    assert "runningRows(" in body

    live = _body("runningRows")
    assert "state.running_targets" in live
    assert "one.root" in live
    assert "one.session" in live

    assert "RUN_MARK" in _script()


def test_新建会话的入口在顶部() -> None:
    """顶部一个"新建会话"入口：会话服务端没有"建会话"端点（会话在第一次真跑起来
    时才落进会话表），所以这里只做"把目标切到一个还没跑过的新名字"。"""
    assert 'id="new-session"' in HTML
    assert "新建会话" in HTML

    body = _body("newSession")
    assert "switchTarget({session: clean})" in body
    assert "readonly" in body, "只读壳里不许摆能点的入口"
    assert "pinned" in body, "--root 钉住时只有一个目标，建不了新的"
    assert "newBtn.onclick = newSession" in _script()


def test_窄屏时左栏是可收起的抽屉() -> None:
    """手机宽度可用：左栏收起成抽屉，正文占满。"""
    assert "max-width:860px" in HTML
    assert "body.drawer #sidebar" in HTML
    body = _body("openDrawer")
    assert "classList.add('drawer')" in body
    close = _body("closeDrawer")
    assert "classList.remove('drawer')" in close
    assert "closeDrawer()" in _body("switchTarget"), "切完目标要顺手把抽屉收掉"
    assert "scrim.onclick = closeDrawer" in _script()


def test_只读时左栏的行与新建入口都点不动() -> None:
    """只读壳里点一下只会得到 403（服务端那道才是权威），页面得先说清楚。

    "点不动"落在 `LOCK` 这个类上（CSS 的 pointer-events），而不是把行藏起来：
    人要看得到"这里有哪几条"。钉住的壳（--root）走的是同一个类。
    """
    assert "readonly" in _body("newSession")
    assert "newBtn.disabled = dead" in _body("drawTarget")
    assert "classList.toggle(LOCK, locked)" in _body("renderSidebar")
    target_body = _body("target")
    assert "LOCK" not in target_body


# --- 中间对话流：气泡方向 ---


def test_用户与助手的气泡方向是分开的() -> None:
    """用户右对齐、助手左对齐（宽屏留白更像文档流）。"""
    # 类名是 CSS 的挂点：改了它们，方向就没了
    assert ".msg.user" in HTML
    assert ".msg.assistant" in HTML
    assert "align-self:flex-end" in HTML
    assert "align-self:flex-start" in HTML

    add = _body("add")
    assert "makeBubble('user'" in add
    assert "makeBubble('assistant'" in add
    # 用户那句话走 'me' 这条分支，助手的话走 'say'
    assert "indexOf('me')" in add
    assert "indexOf('say')" in add
    say = _body("say")
    assert "add('▶ ' + goal, 'me')" in say, "用户消息没有走用户那一支"

    # 助手的话可以带代码块
    assert "fillRich" in add
    history = _body("drawHistory")
    assert "item.role === 'user' ? 'user' : 'assistant'" in history


def test_历史也按气泡方向画() -> None:
    body = _body("drawHistory")
    assert "makeBubble(" in body
    assert "fillRich(" in body
    assert "historyDrawn" in body, "历史画两遍会把对话流撑爆"


def test_代码块等宽且能一键复制() -> None:
    """代码块用等宽字体，并带一个复制按钮（复制失败也要说话）。"""
    assert "var(--mono)" in HTML
    assert ".copy-btn" in HTML

    body = _body("fillRich")
    assert "text.split('```')" in body, "没有把代码围栏认出来"
    assert "codeEl.textContent = payload" in body, "代码没有按文本写进去"
    assert "copyText(" in body
    assert "copy" in body

    script = _script()
    assert "navigator.clipboard" in script
    assert "复制" in script


# --- 滚动：跟到底，但不抢 ---


def test_默认滚到底但人不被抢滚动() -> None:
    """自动跟到底；人往上翻时**不要抢**，改为显示「回到最新」。"""
    add = _body("add")
    assert "atBottom()" in add
    assert "if (stick) { scrollDown(); }" in add, "不是先问再滚：会把正在读的人弹回底部"
    assert "if (stick) { scrollDown(); }" in _body("drawHistory")
    down = _body("scrollDown")
    assert "stream.scrollTo" in down
    assert "stream.scrollHeight" in down

    flip = _body("updateToBottom")
    assert "classList.toggle('show'" in flip
    assert "stream.onscroll = updateToBottom" in _script()

    assert 'id="toBottom"' in HTML
    assert "#toBottom" in HTML and ".show" in HTML
    click = _script()[_find("toBottomBtn.onclick") :][:220]
    assert "scrollDown()" in click, "按钮摆出来却回不到最新"


# --- 正在思考 ---


def test_等待模型时有正在思考的指示() -> None:
    """等待时显示动效指示，而且由**既有**的 wait 事件驱动（不新造协议）。"""
    assert 'id="thinking"' in HTML
    assert "#thinking" in HTML
    assert "正在思考" in HTML
    assert "thinking-dots" in HTML

    block = _body("setThinking")
    assert "classList.toggle('show'" in block

    wait_at = _script().find("data.type === 'wait'")
    assert wait_at != -1
    wait_block = _script()[wait_at : wait_at + 400]
    assert "setThinking(true)" in wait_block, "wait 心跳没有把指示点亮"

    final_at = _script().find("data.type === 'final'")
    assert final_at != -1
    assert "setThinking(false)" in _script()[final_at : final_at + 120]


# --- 底部输入条 ---


def test_底部是输入条加发送键() -> None:
    assert 'id="composer"' in HTML
    assert 'id="goal"' in HTML
    assert 'id="send"' in HTML
    assert "Enter 发送" in HTML
    assert "Shift+Enter 换行" in HTML


def test_Enter发送与ShiftEnter换行() -> None:
    body = _body("onKeydown")
    assert "event.key !== 'Enter'" in body
    assert "event.shiftKey" in body, "没挡住 Shift+Enter，换行会变成发送"
    assert "preventDefault()" in body
    assert "say()" in body
    assert "goalBox.onkeydown = onKeydown" in _script(), "处理函数写了却没绑上"


def test_发送键走同一个run() -> None:
    say = _body("say")
    assert "post('/run', {goal})" in say
    assert "sendBtn.onclick = say" in _script(), "发送按钮没绑上"
    # 只读壳里不许发：非回环按钮点了只会 403
    assert "if (readonly) { return; }" in say


def test_运行中发送键禁用并说明原因() -> None:
    """后端没有停止接口（只有 /run 与 /confirm）：不摆一个点了就骗人的停止键，
    而是把发送降级成"跑完再发"，并把原因写出来。

    证伪法：让 syncSend 不再看 busy（运行中仍然可点），这条就红。
    """
    send = _body("syncSend")
    assert "sendBtn.disabled = busy" in send

    target = _body("drawTarget")
    assert "没有停止" in target, "禁用了却不说明原因，那个灰按钮看起来就是坏了"

    final_at = _script().find("data.type === 'final'")
    assert "setRunning(false)" in _script()[final_at : final_at + 160], "跑完没有把发送放回可用"


# --- 主题与响应式 ---


def test_主题变量表一处定义() -> None:
    """颜色全在变量表里，默认深色、跟随系统；改一处就整体换掉。"""
    assert ":root {" in HTML
    assert "@media (prefers-color-scheme: light)" in HTML
    assert "color-scheme" in HTML
    for name in ("--bg", "--text", "--panel", "--accent", "--bubble-user"):
        assert (name + ":") in HTML, f"主题变量 {name} 没有定义"

    # 变量表要盖住结构里的关键颜色：写死颜色会让"换主题"半截
    style = HTML[HTML.find("<style>") : HTML.find("</style>")]
    assert "#212121" in style, "默认那一档要是深色（ChatGPT 观感）"
    assert "background:var(--bg)" in style


def test_手机上输入条不被键盘顶掉() -> None:
    """`interactive-widget=resizes-content` + 固定的 body：软键盘弹起时输入条跟着走。"""
    assert "interactive-widget=resizes-content" in HTML
    assert "viewport-fit=cover" in HTML
    assert "env(safe-area-inset-bottom)" in HTML
    assert "overflow:hidden" in HTML


# --- 既有行为一条都不许丢 ---


def test_只读时发送键不出现也不可点() -> None:
    """只读壳里没有可点的东西：点一下被 403 拒，只会让人以为壳坏了。

    判据两点：① 发送的处理函数第一句就是只读早退；② 只读状态由**服务端**的
    `state.readonly` 定，而且它一到位就把发送键关掉。
    """
    say = _body("say")
    assert "if (readonly) { return; }" in say

    view = _body("showReadonly")
    assert "state.readonly" in view
    assert "readonly = Boolean(state.readonly)" in view

    send = _body("syncSend")
    assert "readonly" in send

    # 运行中不许发，只读同样不许发：两种都要落在同一个禁用条件上
    assert "busy || readonly" in send


def test_只读时审批按钮也不摆出来() -> None:
    body = _body("showActions")
    assert "if (readonly) { return; }" in body


def test_pinned时选择也点不动() -> None:
    """`--root` 钉住的壳只有一个目标：两段列表只读 + 说明原因。

    `test_page_targets.py` 里也有一条（量的是两条下拉）；这里管的是**新加的
    两段列表与新建入口**同样要认这个判据——只把下拉灰掉、行却还能点，
    点下去就是一次 409，人只会以为是坏的。
    """
    body = _body("drawTarget")
    assert "t.pinned" in body
    assert "wsBox.disabled = locked" in body
    assert "newBtn.disabled = dead" in body
