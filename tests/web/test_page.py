"""页面的结构层断言：没有浏览器驱动，只能量 HTML/JS 文本里的**分支**。

⚠️ 这一层**证伪不了视觉**（颜色、间距、圆角、响应式断点这些只能人眼看，见交付
说明里"哪些没能真在浏览器里看过"）。所以每条断言都钉在一件**去掉就坏**的事上：
某个容器没了、某条分支没了、某个类名改了、某个处理函数没被绑上。

唯一能看见"脚本真的是脚本"的地方是 `test_内联脚本能过node语法检查`：
它把内联 JS 抽出来交给 `node --check`。文本层量不出来的东西（少一个括号、
少一个引号、`\\n` 写成真换行）在那里会红。
"""

import re
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
    """ChatGPT 那套：左边一条会话栏，按工作区分段，段里是它的会话。

    行的**更新方式**在这一批改了：以前每一帧都 `replaceChildren()` 把两段整段
    推倒重画（那是"切换很卡"的一半），现在按 spec 同步（`syncGroup`），
    签名没变就一个节点都不动。行为数字钉在 `test_page_dom.py` 里。
    """
    assert 'id="sidebar"' in HTML
    assert 'id="workspaces"' in HTML
    assert 'id="sessions"' in HTML

    body = _body("renderSidebar")
    # 两段各自的名字，以及"每一段都要有列表"这件事
    assert "工作区" in body
    assert "会话" in body
    assert "syncGroup(workspaceList" in body
    assert "syncGroup(sessionList" in body
    assert "sidebarSig" in body, "没有「同键跳过」：同一帧重复到达会整段重画"
    # 当前目标要能一眼看出来（高亮）
    assert "classList.add('on')" in _body("row")
    assert "markChosen()" in body, "行的复用之后要重新点亮选中态"


def test_列表按行复用而不是整段重画() -> None:
    """一行先描述成 spec（文字 / 在不在跑 / 那个小按钮），只有 sig 变了才建节点。

    证伪法：把 `syncGroup` 换回 `replaceChildren()`，`test_page_dom.py` 里
    那三条"重复帧 / 补帧 / 补历史"立刻变红。
    """
    body = _body("syncGroup")
    assert "have._sig === spec.sig" in body, "没有比对签名，等于每次都重建"
    assert "box.replaceChild(node, have)" in body
    assert "box.appendChild(node)" in body

    spec = _body("rowSpec")
    assert "JSON.stringify([" in spec
    # 「亮不亮」不进签名：切一行的高亮只改类，不该把节点重建一遍。
    assert "Boolean(on)" not in spec


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
    # 两条都要派发：原先只派发会话那条，于是**点工作区那一列什么都没发生**
    # （ws 那条 change 监听永远收不到事件，"点了没反应"就是这么来的）。
    assert "wsBox.dispatchEvent" in dispatch
    assert "sessBox.dispatchEvent" in dispatch
    # 目标名不一定已经是控件里的一项（当前会话没跑过就不在会话表里）：
    # 不先把那项补出来，赋 value 会被浏览器丢掉，点一行就变成"不改会话"。
    assert "pick(" in dispatch
    assert "box.value = value" in _body("pick")

    script = _script()
    assert "wsBox.addEventListener('change'" in script
    assert "sessBox.addEventListener('change'" in script


def test_点一行立刻亮起来不等回帧() -> None:
    """回帧那一趟里服务端会顺带探活模型，模型不通时要好几秒。

    所以点了就先亮（`chosen` 是乐观选中），回帧到了由 `applyState` 交棒给
    服务端确认的目标；切换被拒时拨回去。
    """
    body = _body("switchTarget")
    assert "chosen = wantedKey(payload)" in body
    assert "markChosen()" in body
    # 失败要拨回当前目标（与"选择栏拨回去"同一件事，只是这次是行的高亮）。
    assert "drawTarget(lastState)" in body

    apply_body = _body("applyState")
    assert "chosen = {root: '', session: ''}" in apply_body, "回帧之后要交棒"
    assert "markChosen()" in apply_body

    want = _body("wantedKey")
    assert "payload.workspace" in want
    assert "payload.session" in want


def test_切换完成后服务端补的那一帧不再重建侧栏() -> None:
    """同一帧重复到达要**一个节点都不重建**（行为数字在 `test_page_dom.py`）。"""
    assert "if (sig === sidebarSig) { return; }" in _body("renderSidebar")
    assert "if (box.dataset.fill === sign)" in _body("fillSelect")


def test_左栏标出哪个目标在跑() -> None:
    """切走不杀任务（设计 §3.4）：正在跑的目标必须标出来。

    判据只有 `state.running_targets` 一处（`running` 那个布尔值只讲当前目标）。
    """
    body = _body("renderSidebar")
    assert "runningRows(" in body

    live = _body("runningRows")
    assert "state.running_targets" in live
    assert "one.root" in live
    assert "one.session" in live

    # 点亮的那个小圆点是行的一部分（跟着行一起复用 / 重建），不是外挂上去的。
    assert "markRunning(line, Boolean(running))" in _body("row")
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


def test_历史也只追加不重画() -> None:
    """同一个目标上，快照里的往来会变长：只补没画过的那一段。

    改之前是"画过就不再画"（`historyDrawn` 一个布尔值），于是**多出来的那几条
    永远不出现**——数据变了却不重绘，与"数据没变却重绘"正好是一对反的错。
    """
    body = _body("drawHistory")
    assert "makeBubble(" in body
    assert "fillRich(" in body
    assert "for (let index = drawn; index < items.length; index += 1)" in body, (
        "不是从「已经画到哪儿」接着画：那是全量重画"
    )
    assert "drawn = items.length" in body
    assert "drawn > items.length" in body, "列表变短时要能重来一遍"

    reset = _body("resetView")
    assert "drawn = 0" in reset, "换了目标要重新开始画"


def test_代码块等宽且能一键复制() -> None:
    """代码块用等宽字体，并带一个复制按钮（复制失败也要说话）。"""
    assert "var(--mono)" in HTML
    assert ".copy-btn" in HTML

    body = _body("fillRich")
    assert "text.split('```')" in body, "没有把代码围栏认出来"

    block = _body("codeBlock")
    assert "codeEl.textContent = payload" in block, "代码没有按文本写进去"
    assert "copyText(" in block
    assert "copy" in block

    script = _script()
    assert "navigator.clipboard" in script
    assert "复制" in script


# --- ③ 代码块与正文分得开 ---


def test_围栏是代码而其余是正文() -> None:
    """`split('```')` 出来的**奇数块**才是代码（围栏一定成对）。

    改之前每一块都被当代码块画（连正文也是）：整条消息看上去全是等宽的——
    "代码与对话没区分开"的根就在这里。行为数字在 `test_page_dom.py`。
    """
    body = _body("fillRich")
    assert "index % 2 === 1" in body, "没有按奇偶把代码和正文分开"
    assert "bubble.appendChild(codeBlock(parts[index]))" in body
    assert "appendProse(bubble, parts[index])" in body

    prose = _body("appendProse")
    assert "index % 2 === 1" in prose, "行内代码没有按 ` 切出来"
    assert "inline.className = 'inline-code'" in prose
    assert "document.createTextNode(piece)" in prose, "正文要按文本节点写进去"


def test_代码块有明显的块样式且不撑破气泡() -> None:
    """视觉判据只能钉在 CSS 上（真长什么样得人眼看，见交付说明）。"""
    style = HTML[HTML.find("<style>") : HTML.find("</style>")]
    # 底色 / 边框 / 圆角 / 内边距：四样都在，才叫"一块代码"
    assert ".code {" in style
    assert "background:var(--code-bg)" in style
    assert "border:1px solid var(--code-line)" in style
    assert "border-radius:10px" in style
    assert "padding:11px 13px" in style
    # 自己滚，不撑破气泡
    assert "overflow:auto" in style
    assert "max-height:360px" in style
    # 气泡与消息必须是 min-width:0，否则 flex 子项不肯收缩，代码会把气泡撑爆
    assert "min-width:0" in style
    # 语言标签可以让位、复制键位置固定
    assert ".code-lang" in style and "text-overflow:ellipsis" in style
    assert "flex:0 0 auto" in style
    # 行内代码与正文要能区分
    assert ".inline-code" in style
    assert "background:var(--inline-bg)" in style
    assert ".prose" in style
    # 语言标签与"代码"兜底两种文案都在
    script = _script()
    assert "'代码'" in script or '"代码"' in script


def test_主题变量表两档都给代码与滚动条的色() -> None:
    style = HTML[HTML.find("<style>") : HTML.find("</style>")]
    dark = style[: style.find("@media (prefers-color-scheme: light)")]
    light = style[style.find("@media (prefers-color-scheme: light)") :]
    for name in ("--code-bg", "--code-line", "--code-bar", "--inline-bg",
                 "--inline-line", "--scroll-thumb", "--scroll-thumb-hover"):
        assert (name + ":") in dark, f"深色那一档缺 {name}"
        assert (name + ":") in light, f"浅色那一档缺 {name}"


# --- ④ 滚动条 ---


def test_滚动条按平台美化而不是藏掉滚动能力() -> None:
    """两套写法都要给：WebKit 认 `::-webkit-scrollbar`，Firefox 认标准属性。

    "隐藏"这一条要小心：`display:none` 真的会让鼠标用户没有抓手。这里做的是
    美化（细、跟主题、悬停才亮），可滚性一个字都没动。
    """
    style = HTML[HTML.find("<style>") : HTML.find("</style>")]
    assert "::-webkit-scrollbar {" in style
    assert "::-webkit-scrollbar-thumb" in style
    assert "::-webkit-scrollbar-track" in style
    assert "scrollbar-width:thin" in style
    assert "scrollbar-color:var(--scroll-thumb) transparent" in style
    # 不许把滚动条藏掉：藏掉之后列表与代码块仍然要能滚。
    assert "::-webkit-scrollbar { display:none" not in style
    assert "::-webkit-scrollbar{display:none" not in style
    assert "::-webkit-scrollbar-thumb { display:none" not in style
    # 两个滚动区自己也要能滚（美化不许把它一起改掉）
    assert "#stream {" in style and "overflow-y:auto" in style
    assert "#panel {" in style and "overflow-y:auto" in style
    # 能滚的还有代码块（第一层是美化，不是"藏了就不管了"）
    assert "overflow:auto" in style


# --- ② 连不上模型时说人话 ---


def test_失败在对话流里说人话而不是一行原始异常() -> None:
    """`final` 失败与 `error` 事件都走 `showFailure`（不再直接 `add` 原始文本）。

    行为数字（地址 / 可能原因 / 下一步）在 `test_page_dom.py`。
    """
    body = _body("onEvent")
    assert "showFailure('失败：', data.text, data.error)" in body, "失败还是一行原始异常"
    assert "showFailure('错误：', data.message, data.error)" in body, "错误事件没有走这条"

    # 认不出来时仍按**原来的画法**：那个词还是原来的词（失败 / 错误）。
    assert "add(head + redact(text), 'bad')" in _body("showFailure")

    explain = _body("explainFailure")
    assert "失败环节" in explain
    assert "模型地址" in explain
    assert "可能原因" in explain
    assert "下一步" in explain
    # 下一步必须是**真的存在**的命令（这一批的硬要求）：`spool service list`
    # 看本机模型配方、`spool config` 看 provider/base_url 从哪来。
    assert "spool service list" in _script()
    assert "spool config" in _script()


def test_下一步里指的命令真的存在(capsys) -> None:
    """比"文本里有这几个字"强一档：指路要指**真的**（命令改名/删掉时这条会红）。

    页面里出现的每个 `spool <子命令>` 都要能在 CLI 里找到——写一句
    `spool status` 之类不存在的命令，等于把人指进死路。
    """
    from spoolkit.cli import app as cli_app

    used = set(re.findall(r"spool ([a-z][a-z0-9-]*)", _script()))
    assert used, "脚本里一个 spool 命令都没提到"

    with pytest.raises(SystemExit):
        cli_app.main(["--help"])
    text = capsys.readouterr().out
    match = re.search(r"\{([a-z0-9,-]+)\}", text)
    assert match, "没从 CLI 的帮助里抓到子命令清单（帮助的格式变了？）"
    known = set(match.group(1).split(","))

    assert used <= known, f"指了一个不存在的命令：{sorted(used - known)}"


def test_结构化线索只增不改() -> None:
    """页面优先用服务端**只增**的那条字段（`error: {stage, url, kind}`），
    没有它时按错误文本自己认——两条路都要在。"""
    body = _body("explainFailure")
    assert "info.kind || failureKind(text)" in body
    assert "info.url" in _body("modelAddress")

    script = _script()
    assert "data.error" in script
    assert "stage" in script


def test_页面上的错误文本先遮蔽密钥() -> None:
    """密钥、Bearer 串、KEY=VALUE 不许出现在页面上。"""
    body = _body("redact")
    assert "sk-" in body
    assert "Bearer" in body
    assert "token" in body and "secret" in body and "password" in body
    assert "***" in body
    # 画失败那块也要过一遍遮蔽（原始信息那一行最容易带出地址与参数）。
    assert "redact(usefulLine(text))" in _body("explainFailure")
    assert "redact(" in _body("showHealth")


# --- ① 删除会话 ---


def test_会话行上有移除入口而且要两次点击() -> None:
    """一点就删太容易误伤：第一下只把这一行变成确认态（`armed`），第二下才发请求。"""
    actions = _script()
    assert "'/session/delete'" in actions
    assert "'/session/restore'" in actions
    assert "requestRemove" in actions and "requestRestore" in actions

    remove = _body("requestRemove")
    assert "if (armed !== value)" in remove, "没有二次确认：点一下就删了"
    assert "markArmed()" in remove
    assert "act('/session/delete'" in remove

    armed = _body("markArmed")
    assert "'确认移除'" in armed
    assert "'移除'" in armed
    assert "classList.toggle('armed', ready)" in armed

    # 按钮是行里的一个 button，点它不许当成"切到这一条"
    button = _body("rowAction")
    assert "event.stopPropagation()" in button
    assert "button.dataset.value = value" in button


def test_已移除的会话在页面上能放回来() -> None:
    """"可恢复"在界面上必须够得着，否则不叫可恢复。"""
    body = _body("renderSidebar")
    assert "sremoved" in body
    assert "已移除（" in body
    assert "'恢复'" in body

    target = _body("drawTarget")
    assert "state.sessions_removed" in target


def test_只读时移除入口不摆出来() -> None:
    body = _body("renderSidebar")
    assert "const removable = !locked && !readonly;" in body
    assert "readonly ? null : actSpec('恢复'" in body
    remove = _body("requestRemove")
    assert "if (readonly) { return; }" in remove
    restore = _body("requestRestore")
    assert "if (readonly) { return; }" in restore


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
