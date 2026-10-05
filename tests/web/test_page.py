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
    """待确认不再是 HTML 里的常驻占位 + 常驻按钮条，而是**有待确认内容才建**的一张卡片。
    """
    assert "pending" in HTML
    assert "应用" in HTML
    assert "拒绝" in HTML
    assert "showPending" in HTML
    assert ".ask-card" in HTML, "没有那张卡片的样式挂点"
    # 不许有 DOM 占位：空框本身会让"有东西在等我"，而空按钮还能被 Tab 到。
    assert 'id="tray"' not in HTML
    assert 'id="pending"' not in HTML
    assert 'id="actions"' not in HTML
    assert 'id="apply"' not in HTML
    assert 'id="reject"' not in HTML
    # 卡片挂在**对话流**里（不是浮在输入条上方的常驻横幅）。
    assert "appendNode(node)" in _body("ensureCard")


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
    """配对是待批准；模型未连接是另一条，不能复用「应用/拒绝」。

    它现在是输入条那一行的**状态徽标**（+ 点开的详情），不是顶部横幅、也不是气泡：
    常态下只剩一枚小圆点，不通才变色并**带字**——一枚沉默的绿点会把"未连接"藏起来。

    这里钉的是**要点**（状态词是可判定的名词 + 详情那段复用同一套说法），
    不是整句字面：口径改成"未连接 / 连接超时"时这条不该跟着碎掉，
    而"异常不带字"这类回退必须照样红。
    """
    assert "showHealth" in HTML
    assert 'id="st-health"' in HTML
    assert "未连接" in HTML
    assert "state.health" in HTML
    # 三个短标签都在：未连接 / 连接超时 / 未探测（可判定的状态词，不是口语）。
    body = _body("showHealth")
    assert "模型未连接" in body
    assert "模型连接超时" in body
    assert "'未探测'" in body
    # 详情那几句复用 `explainFailure` / `modelDownLine`（不另写一套说法）。
    assert "explainFailure(" in body, "探活那条没有复用失败那套说法"
    assert "modelDownLine(" in body


def test_页面也报外接配方断开() -> None:
    """llama / 隧道是本机配方，不通时进同一个状态详情，不另开一枚徽标。

    钉要点：**哪一个**外接服务没连上说得出（名字 + 地址在），而且它进的是
    探活那一枚徽标的详情（`healthView.lines`），不是另开一枚徽标。
    """
    assert "services" in HTML
    assert "未连接" in HTML
    body = _body("showHealth")
    assert "item.ok === false" in body
    assert "外接服务未连接" in body
    assert "healthView.lines" in body


def test_探活详情说得出什么时候探的与未探测() -> None:
    """探活挪到后台之后（`web/probe.py`），`/state` 给的是**最近一次**结果。

    所以详情必须说得出"3 秒前"（陈旧值可以接受，**静默的**陈旧值不行），
    而"未探测"既不是通也不是断——三条分支缺一个就红。行为数字（那些文字真的
    被画进了详情）在 `test_page_dom.py` 里。

    这一层钉**要点**：时间戳与陈旧度都在、状态词分得开、`slow` 与不通不许合成
    一句、时间戳缺失时不许冒充"未探测"。整句字面不再钉。
    """
    body = _body("showHealth")
    assert "probedAgo(info)" in body, "说了不通却不说这是什么时候探的"
    assert "model.kind || failureKind(model.detail)" in body, (
        "没用服务端只增的 kind：慢与不通就分不开了"
    )
    assert "state === 'unknown'" in body, "未探测那一档没有分支"
    assert "本机模型：未探测" in body

    ago = _body("probedAgo")
    assert "age_s" in ago
    # 陈旧度：秒与分钟两档都要说得出（措辞是"（3 秒前）"这一类）。
    assert "' 秒前）'" in ago and "' 分钟前）'" in ago
    # 有结果却没时间戳时**不许**说"未探测"：那句话会和"状态：正常"打架。
    assert "时间戳缺失" in ago

    down = _body("modelDownLine")
    assert "连接超时" in down, "慢与不通被合成了一句"
    assert "模型未连接" in down

    # 详情里"这是什么时候探的"用的是服务端给的 `probed_at`（那一刻的墙钟）。
    stamp = _body("probedStamp")
    assert "probed_at" in stamp and "toLocaleTimeString(" in stamp
    # 页面上给的必须是**时刻**：光有"3 秒前"说不清是哪一次探的。
    lines = _body("healthLines")
    assert "'最近一次探测：'" in lines and "probedStamp(info)" in lines


def test_页面按快照显示待批准的配对码() -> None:
    """给人看的一条状态。不能复用「应用/拒绝」——那是落盘确认。

    配对码收进输入条那一行的徽标，而且**只有一条时把码直接写在徽标上**：
    人得把它抄进终端，藏进详情就等于没提示。
    """
    assert "pairings" in HTML
    assert "showPairings" in HTML
    assert "spool approve" in HTML
    assert 'id="st-pair"' in HTML
    assert "/state" in HTML
    body = _body("showPairings")
    assert "drawStatus()" in body, "配对变了没重画状态区"
    section = _body("pairSections")
    assert "spool approve ' + item.code" in section
    assert "配对码：" in section, "详情里没把码写全"
    # 钉要点：那两个按钮**不批配对**这件事必须说得出（口径可以换词，这条事实不行）。
    assert "不批准配对" in section, "没说清那两个按钮不批配对"
    assert "批准在终端" in section, "没说清批配对到底在哪做"
    assert "'配对码 ' + pairingRows[0].code" in _body("drawStatus"), (
        "只有一条配对时码没有直接写在徽标上"
    )


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


def test_等待心跳不占用探活那条通知() -> None:
    """探活那条（模型连不上）不是"还在生成"：两者混在一起，人会以为模型挂了。"""
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
    """顶部一个「＋ 新建会话」：先让服务端**真把会话建出来**（`POST /session/new`
    → 既有的 `kernel_sessions.create_session`），再切过去。

    这条断言是**跟着行为改的**：以前这里只做"把目标切到一个还没跑过的新名字"，
    而 `/target` 不新建会话（`resolve_session` 不认识就报错、且绝不静默新建）
    ——于是那个「＋」点下去只有 400。端到端那几条在 `test_session_new.py` 里，
    这里只管"接线的顺序对"：**先调端点、再切目标**。
    """
    assert 'id="new-session"' in HTML
    assert "新建会话" in HTML

    body = _body("newSession")
    assert "post('/session/new'" in body, "没有真的建会话：又回到「只换个名字」"
    assert "switchTarget({session: made.session})" in body, "建完没有切过去"
    assert body.find("post('/session/new'") < body.find("switchTarget("), (
        "顺序反了：切目标时那条会话还没建出来（服务端会 400）"
    )
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
    assert "add(goal, 'me')" in say, "用户消息没有走用户那一支"

    # 助手的话可以带代码块
    assert "fillRich" in add
    history = _body("drawHistory")
    assert "historyCard(" in history, "历史没有走「按往来画一张卡片」那条路"
    card = _body("historyCard")
    assert "item.role === 'user'" in card
    assert "makeMsg(mine ? 'user' : 'assistant')" in card


def test_历史也只追加不重画() -> None:
    """同一个目标上，快照里的往来会变长：只补没画过的那一段。

    改之前是"画过就不再画"（`historyDrawn` 一个布尔值），于是**多出来的那几条
    永远不出现**——数据变了却不重绘，与"数据没变却重绘"正好是一对反的错。
    """
    body = _body("drawHistory")
    assert "historyCard(" in body
    assert "for (let index = drawn; index < items.length; index += 1)" in body, (
        "不是从「已经画到哪儿」接着画：那是全量重画"
    )
    assert "drawn = items.length" in body
    assert "drawn > items.length" in body, "列表变短时要能重来一遍"
    # 这一批多了一道**认领**：这一轮已经实时画过的那些不新建节点（否则同一句话
    # 会出现两遍）。行为数字在 `test_page_dom.py` 的 `reply_card` 那一组。
    assert "adoptMessage(" in body, "快照里那条已经画过的消息没有认领回来"

    card = _body("historyCard")
    # 报告上屏那一处从 `fillRich` 换成了 `fillReport`：后者按 `详情：` 把报告
    # 分成"结论段进气泡 / 详情进轨迹"两档。**同一个要求**（正文必须画出来）
    # 一个字没放宽，量的落点跟着换了。
    assert "fillReport(" in card and "item.content" in card, "历史正文没画出来"

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


# --- ① 通知：右上角的浮动气泡，两类分得清 ---


def _rule(selector: str) -> str:
    """把样式表里 `selector { … }` 那一条抠出来（这几条里没有嵌套花括号）。"""
    style = HTML[HTML.find("<style>") : HTML.find("</style>")]
    at = style.find(selector + " {")
    assert at != -1, f"样式表里没有 {selector}"
    return style[at : style.find("}", at) + 1]


def test_通知是右上角的浮动气泡而不是横幅() -> None:
    """失败与提示以前挤在同一条横幅带里：一条早就没意义的旧提示会把新错误挤下去，
    而它自己不会走。现在通知走 `#toasts`——右上角那一列浮层的下半截。

    那一列（`#corner`）是**整块 fixed**：常驻提示在上、通知在下，两样都脱离文档流，
    所以"通知把对话流挤下去"与"通知盖住状态徽标"这两件事同时不存在。
    """
    assert 'id="toasts"' in HTML
    assert 'id="corner"' in HTML
    corner = _rule("#corner")
    assert "position:fixed" in corner, "右上角那一列没有浮起来"
    assert "top:" in corner and "bottom:" not in corner, "锚在底部会压住输入条"
    # 常驻提示在前、通知在后：同一列里靠右堆叠，谁也不会盖住谁。
    assert "flex-direction:column" in corner, "那一列不是竖着排的"
    assert "gap:8px" in corner, "两块之间没有间隙"
    toasts = _rule("#toasts")
    assert "flex-direction:column" in toasts, "通知没有排成一列（会互相盖住）"
    # 单个气泡自己不许绝对定位或固定定位：那是"叠在一起"的写法。
    assert "position:absolute" not in _rule(".toast")
    assert "position:fixed" not in _rule(".toast")
    # 横幅带上只剩"这是哪一种壳 / 有没有目标"这类模式说明。
    for gone in ('id="health"', 'id="pairing"', 'id="elsewhere"'):
        assert gone not in HTML, f"{gone} 还留在横幅带上"


# --- ① 通知**不许把页面挤下去**：脱离文档流、不在滚动容器里 ---

# 这些标签没有结束标签；手写标签栈要把它们跳过去，否则栈会一路歪掉。
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


def _static_html() -> str:
    """静态那一半（`<script>` 之前）。脚本里全是 JS 字符串里的 `<`，
    拿它一起扫标签栈会被内联 SVG 搅乱——而通知在不在滚动容器里，只看静态这一半。"""
    return HTML[: HTML.find("<script>")]


def _ancestors(element_id: str) -> list:
    """`id="…"` 那个元素在静态 HTML 里的祖先标签（从外到内，不含它自己）。

    元素用 `标签名#id` 表示（没有 id 就是标签名）。手写标签栈而不是引解析器：
    这一页是单文件、结构就那几种标签，够用；而"通知的父节点是不是 `body`、
    它在不在滚动容器里"**只有**这一层能量——假 DOM 不解析静态树（它按 id 造空壳）。
    """
    static = _static_html()
    # 重复的 id 一律是 bug（页面里没有一件东西该出现两次），而它会让下面这个
    # "取第一个匹配"的扫描失去意义：先把这件事证伪掉。
    assert static.count(f'id="{element_id}"') == 1, (
        f'id="{element_id}" 在静态 HTML 里出现了 '
        f"{static.count(f'id=\"{element_id}\"')} 次"
    )
    stack = []
    for match in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>", static):
        closing, name, rest = match.group(1), match.group(2).lower(), match.group(3)
        if closing:
            while stack and stack.pop()[0] != name:
                pass
            continue
        if name in _VOID_TAGS or rest.rstrip().endswith("/"):
            continue
        if f'id="{element_id}"' in rest:
            return [label for _, label in stack]
        found = re.search(r'id="([^"]+)"', rest)
        stack.append((name, name + ("#" + found.group(1) if found else "")))
    raise AssertionError(f"静态 HTML 里没有 id={element_id!r} 的元素")


def test_提示与通知那一列在body下且不在滚动容器里() -> None:
    """浮层一出现就把页面往下挤，是因为它长在**文档流**里。现在不是了：

    ① 那一列 `position:fixed`（脱离文档流），而且 `pointer-events:none`
       ——它跟内容重叠时，底下的按钮照样点得动（徽标 / 面板 / 气泡自己吃点击）；
    ② 它的父节点就是 `body`，不在 `#stream` / `#thread` / `#body` 任何一个里面，
       也就是**不在滚动容器里**：滚对话流不会把它带走，它也不占对话流的位置。
       常驻提示（`#hints`）与通知（`#toasts`）都在这一列下，两样都受益。

    证伪法：把 `#corner` 那个 div 挪进 `#stream` 里（或把 `position:fixed` 删掉），
    这里立刻红——而"提示 / 通知不动对话流"的行为断言在 `test_page_dom.py` 里。
    """
    assert 'id="corner"' in HTML
    corner = _rule("#corner")
    assert "position:fixed" in corner, "右上角那一列没有脱离文档流"
    assert "pointer-events:none" in corner, "那一列会挡住底下那层内容的点击"

    assert _ancestors("corner") == ["html", "body"], (
        f"那一列不是挂在 body 下：{_ancestors('corner')}"
    )
    assert _ancestors("hints") == ["html", "body", "div#corner"], (
        f"常驻提示不在那一列里：{_ancestors('hints')}"
    )
    assert _ancestors("toasts") == ["html", "body", "div#corner"], (
        f"通知不在那一列里：{_ancestors('toasts')}"
    )
    # 反证：对话流确实深深嵌在滚动容器里（不然上一条会因为"页面啥都没有"而假绿）。
    assert _ancestors("stream") == ["html", "body", "div#body", "div#thread"], (
        f"对话流的层次变了？{_ancestors('stream')}"
    )
    # 而"输入条那一行"仍然在文档流里（它是这一页唯一还带状态文字的地方），
    # 这正好是"挤不挤"的反证面：徽标要是还在那儿，就会跟着它一起占高度。
    assert _ancestors("composer") == ["html", "body", "div#body", "div#thread"], (
        f"输入条的层次变了？{_ancestors('composer')}"
    )


def test_顶部没有横幅带也没有常驻提示() -> None:
    """顶部那条**在文档流里**的横幅没有了（不是"显示一句一切正常"，而是不占位）。

    以前那条横幅装着：只读（非回环）、`--root` 钉住、没有目标、模型探活。
    它一出现就把对话流往下推。现在这些收进**右上角那一列浮层**（`#corner` 里的
    `#hints` + 详情），顶部只剩"我在哪儿"（`#top` 里的目标）。
    """
    style = HTML[HTML.find("<style>") : HTML.find("</style>")]
    assert "class=\"banner\"" not in HTML, "横幅那个挂点还在页面上"
    assert ".banner {" not in style, "横幅的样式还在"
    for gone in ('id="readonly"', 'id="target-note"', 'id="health"',
                 'id="pairing"', 'id="elsewhere"'):
        assert gone not in HTML, f"{gone} 还常驻在页面上"
    # `#top` 里只有三样：它自己、抽屉开关、当前目标（多一个都算"顶部有提示"）。
    top = HTML[HTML.find('<div id="top">') : HTML.find('<div id="scrim">')]
    assert re.findall(r'id="([^"]+)"', top) == ["top", "menu", "where"], (
        f"顶部多出了东西：{top}"
    )
    assert 'id="where"' in top and 'id="menu"' in top


def test_常驻提示在右上角浮层里而不是占着输入条那一行() -> None:
    """用户报的第一条：常驻提示"占满屏幕顶端、压缩会话窗口"。

    它在文档流里的任何一个位置都会挤对话区（上一轮搬进输入条下面那一行也还是挤：
    徽标一多就换行，`#composer` 变高、`#stream` 变矮）。所以这一轮两件事一起做：

    ① 五枚徽标 + 详情面板全搬进 `#hints`（在 `#corner` 那个 `position:fixed` 的列里）；
    ② 输入条那一行**只剩一句静态提示**（Enter 发送），再没有会随状态长出来的东西。
    """
    # 五枚徽标都在 `#hints` 里（静态结构），而不在输入条那一行里。
    hints = HTML[HTML.find('<div id="hints">') : HTML.find('<div id="toasts">')]
    for badge in ("st-health", "st-mode", "st-target", "st-pair", "st-elsewhere",
                  "st-panel"):
        assert f'id="{badge}"' in hints, f"{badge} 不在右上角那一列里"
    strip = HTML[HTML.find('<div class="st">') :][:400]
    assert "Enter 发送" in strip, "输入条那一行连原来那句提示都没了"
    assert 'id="st-' not in strip, "输入条那一行里还留着会随状态长出来的徽标"
    assert strip.count("<button") == 0, "输入条那一行里还有按钮"

    # 尺寸是**参数化**的：最大宽度写在那一列上，高度自适应（`height:auto`）。
    corner = _rule("#corner")
    assert "width:min(420px, 92vw)" in corner, f"最大宽度没有参数化：{corner}"
    assert "height:auto" in corner, f"高度被写死了：{corner}"
    hints_rule = _rule("#hints")
    assert "flex-wrap:wrap" in hints_rule, "徽标多了不换行，会横向溢出"
    assert "height" not in hints_rule or "auto" in hints_rule, "提示那一行高度被写死了"
    # 详情面板以 `#hints` 为锚（它得是定位祖先），高度自适应、超长才自己滚。
    assert "position:relative" in hints_rule, "详情面板没有可锚定的祖先"
    panel = _rule(".st-panel")
    assert "position:absolute" in panel, "详情面板长在文档流里"
    assert "max-height:56vh" in panel and "overflow-y:auto" in panel


def test_状态区常态只剩一枚圆点() -> None:
    """探活收进一枚小圆点：正常时 `label` 是空的——**不画**"一切正常"那句。

    判据在两条分支上：常态给空标签，异常给短标签（可判定的状态词）；而徽标在右上角
    那一列里（`#hints`），不是输入条那一行——那一行现在只有一句静态提示。
    """
    assert 'id="st-health"' in HTML
    body = _body("showHealth")
    assert "label: ''" in body, "常态还在徽标上写一句话"
    # 异常必须**带字**（不是一枚沉默的绿点），而且 slow 与别的状态分得开。
    assert "label: kind === 'slow' ? '模型连接超时'" in body, (
        "异常没有带字：一枚沉默的绿点会把「未连接」藏起来"
    )
    assert "'模型未连接'" in body, "连接不通那一档没有短标签"
    status = _body("drawStatus")
    assert "healthLabel.classList.toggle('hidden', healthView.label === '')" in status, (
        "常态那枚圆点旁边还挂着空标签"
    )
    # 那一行与"Enter 发送"仍是同一个 flex 行（`.st-hint` 靠 margin-left:auto 靠右）。
    assert "margin-left:auto" in _rule(".st-hint")


def test_状态详情保留四项而且异常仍然看得见() -> None:
    """详情里要有：状态 / 地址 / 探活时间（`probed_at` + `age_s`）/ 可能原因 / 下一步。

    "下一步"必须是一条**真存在**的命令（既有的 `failureNext` 给的是 `spool service
    list` / `spool config` 这类），不能是"请联系管理员"。标签钉的是**要点**
    （地址在、时刻在、原因与下一步接进来了），不是整句字面。
    """
    lines = _body("healthLines")
    assert "'状态：'" in lines
    assert "'模型服务地址：'" in lines, "详情里没有地址那一行"
    assert "'最近一次探测：'" in lines, "详情里没有探活时刻那一行"
    assert "probedStamp(info)" in lines
    assert "healthView.lines" in lines, "可能原因 / 下一步那几行没有接进详情"

    # 异常那一档带着原始信息（先遮蔽密钥），而它是"bad"（红的）。
    body = _body("showHealth")
    assert "cls: 'bad'" in body
    assert "redact(" in body
    # 详情面板是浮层：绝对定位（锚在右上角那一列里），展开/收起都不占文档流。
    assert "position:absolute" in _rule(".st-panel"), "详情面板长在文档流里"


def test_状态详情能收起而且点外面也行() -> None:
    """展开之后得能收起来，而且**焦点不在输入框里时也收得掉**（点过徽标之后焦点
    就在按钮上）：所以除了输入框那条 Esc，还挂着"点那一列之外"与 document 上的 Esc。

    常驻提示搬到右上角之后，"外面"的判据也跟着改了：以前是 `#composer`（徽标在
    输入条那一行里），现在是 `#corner`——不然点一下对话流就会被当成"点在外面"。
    """
    script = _script()
    assert "if (event.key === 'Escape') { setStatusOpen(false); return; }" in _body("onKeydown")
    assert "document.onkeydown" in script and "Escape" in script
    outside = script[script.find("document.onclick = function") :][:400]
    assert "closest('#corner')" in outside, "点外面收不掉"
    assert "setStatusOpen(false)" in outside


def test_只读与钉住的模式信息仍然可发现() -> None:
    """"这一页现在是什么模式"回答的是"为什么点不动"，所以它**不许消失**。

    只是从顶部横幅挪到了输入条那行的锁徽标：悬停看得见标题、点开是全文。
    """
    assert 'id="st-mode"' in HTML
    view = _body("showReadonly")
    assert "state.readonly" in view
    assert "readonly = Boolean(state.readonly)" in view
    assert "readOnlyNote = readonly" in view, "只读那句解释没有落到状态区"
    assert "非回环地址上" in view and "只能看，不能驱动" in view, (
        "只读那句解释被删短了：为什么、能看什么、想驱动怎么办三件事都得在"
    )
    assert "--host" in view
    assert "drawStatus()" in view

    target = _body("drawTarget")
    assert "pinnedNote = locked" in target, "--root 钉住的解释没有落到状态区"
    assert "--root" in target
    assert "drawStatus()" in target

    # 徽标上的短标签由这两件事拼出来（同时成立时说全）。
    badge = _body("modeBadgeText")
    assert "'只读'" in badge and "'已钉住'" in badge
    # 配色：只读是紫的、异常是红的、待配对是黄的（"低调"不等于"看不见"）。
    assert "var(--ro-line)" in _rule(".st-item.ro")
    assert "var(--danger-line)" in _rule(".st-item.bad")


def test_窄屏时提示与通知那一列不越界而且仍然是fixed() -> None:
    """860px 以下左栏收成抽屉；右上角那一列跟着收进来，**但一个 `position` 都不许写**
    ——写上去就等于把它从浮层拉回文档流，"挤下去"那个毛病就回来了。

    "不越界"的判据是**左右都压住**（`left` 与 `right` 同时给、宽度交给 `auto` 算）：
    只锚右边的话，宽度大于视口时左边缘会跑到屏幕外。
    """
    mobile = HTML[HTML.find("@media (max-width:860px)") :][:1600]
    assert "#corner {" in mobile
    assert "left:10px" in mobile and "right:10px" in mobile
    assert "width:auto" in mobile
    rule = mobile[mobile.find("#corner {") : mobile.find("}", mobile.find("#corner {")) + 1]
    assert "position" not in rule, f"窄屏那条规则里写了定位：{rule}"
    # 底座那条规则里的最大宽度是**参数化**的（`min(420px, 92vw)`）：窄屏还有第二道
    # 护栏（上面那对 left/right），两道加起来才不会越界。
    assert "width:min(420px, 92vw)" in _rule("#corner")


def test_减少动效时提示与通知都不动画() -> None:
    """`prefers-reduced-motion` 下不动画（不动画 ≠ 不出现：它只是直接在那里）。"""
    block = HTML[HTML.find("@media (prefers-reduced-motion: reduce)") :]
    block = block[: block.find("</style>")]
    assert ".toast { animation:none; }" in block, f"减少动效那一档没有关掉入场动画：{block}"
    assert ".st-panel { animation:none; }" in block, "状态详情那一档还在动画"
    assert "animation:toast-in" in _rule(".toast"), "入场动画的钥匙丢了"
    assert "animation:toast-in" in _rule(".st-panel"), "详情面板的入场动画钥匙丢了"


def test_自己说的话不带符号前缀() -> None:
    """发出去的那一条原来是 `符号 + 空格 + 原话`。那个符号现在整份文件里一次都不出现，
    也没换来一个更花哨的：气泡底色 + 右对齐本来就说得清"这是我发的"。"""
    assert "\u25b6" not in HTML, "那个三角形前缀又回来了"
    say = _body("say")
    assert "add(goal, 'me')" in say, "发出去的那一条前面又被拼了东西"
    assert "add('" not in say, f"say() 里又手拼了前缀：{say}"


def test_每条通知都能关掉两条以上给全部关闭() -> None:
    """人得能自己把一条通知赶走，不然"能自动消失的那一档"就变成唯一出路。"""
    assert 'id="toast-all"' in HTML
    assert "全部关闭" in HTML
    assert "toastAllBtn.onclick = closeAllToasts" in _script()

    close = _body("closeToast")
    assert "removeChild(" in close, "关掉只是藏起来，节点还留着"
    assert "clearTimeout(" in close, "关掉了却留着那个自动消失的定时器"

    # 关闭键里只有一个图标：得有个读屏念得出来的名字。
    assert "aria-label" in _body("showToast")
    assert "'关掉这条通知'" in _body("showToast")

    # 人点关闭键：记下"这一份内容人不要了"，下一拍轮询别又放回来。
    assert "toastDismissed[key] = toastSigs[key]" in _body("dismissToast")
    assert "if (!have && toastDismissed[key] === sig)" in _body("showToast")

    chrome = _body("syncToastChrome")
    assert "toastOrder.length > 0" in chrome, "一条都没有时那一列还在"
    assert "toastOrder.length > 1" in chrome, "只有一条时就摆出「全部关闭」"


def test_提示自己会消失而错误不许自己消失() -> None:
    """两类：`info` 几秒后自己走；错误 / 警告 / 要人决策的一律留到人关为止。"""
    assert "const AUTO_CLOSE_MS = 5000;" in _script()
    toast = _body("showToast")
    assert "if (spec.kind === 'info')" in toast, "自动消失没有按类分：错误也会自己走"
    assert "setTimeout(" in toast
    # 失败那条是 error：它永远不排自动消失的定时器。
    failure = _body("notifyFailure")
    assert "kind: 'error'" in failure


def test_失败通知复用上一轮那套说法() -> None:
    """四件事（哪一步 / 地址 / 可能原因 / 下一步）由 `explainFailure` 给，
    不另写一套；页面上的原文一律先过 `redact`。
    """
    body = _body("showFailure")
    assert "explainFailure(text, extra)" in body
    assert "notifyFailure(head, text, parts)" in body, "失败只在对话流里躺着，没有弹出来"
    assert "add(head + redact(text), 'bad')" in body, "认不出来的错误改了口径"

    notice = _body("notifyFailure")
    assert "redact(" in notice
    assert "usefulLine(" in notice
    # 请求被拒（409/403）也算失败：摆进对话区的同时弹一条，别只在控制台里丢。
    post = _body("post")
    assert "add('请求被拒绝：' + why, 'bad')" in post
    assert "notifyFailure('请求被拒绝：'" in post


def test_历史不加那句给用户看的说明() -> None:
    """原来这里写着"之前的往来（给人看的，不进模型上下文）"——那是**说给实现者听的**。

    改法：那句话不再出现在页面上；历史消息本身一条都不许少（用户要看得到过去
    说过什么），分隔线留着当视觉界线，历史那几条多一个 `history` 类（略淡）。
    行为数字（4 条历史一条不少、分隔线上一个字都没有）在 `test_page_dom.py` 里。
    """
    script = _script()
    assert "之前的往来" not in script, "那句说给实现者听的话还在页面上"
    assert "不进模型上下文" not in script, "实现细节还写在页面脚本里"

    history = _body("drawHistory")
    assert "history-sep" in history, "历史与这一趟之间的界线没了"
    # 分隔线只是**一条线**：里面一个字都不写（`::before`/`::after` 画的线）。
    assert "textContent" not in history, "分隔线上还写着字"
    # 历史消息照旧画出来，只是多一个类（`historyCard` 是这一批新抽出来的那个落点）。
    card = _body("historyCard")
    assert "makeMsg(" in card
    assert "'history'" in card, "历史没有可辨认的类（略淡那一档）"
    assert "fillReport(" in card and "item.content" in card, "历史正文没画出来"
    assert ".msg.history" in HTML, "没有略淡那一条样式"


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


# --- 生成中指示 ---


def test_等待模型时有生成中的指示() -> None:
    """等待时显示动效指示（状态词是**生成中**，不是拟人的"正在思考"），
    而且由**既有**的 wait 事件驱动（不新造协议）。"""
    assert 'id="thinking"' in HTML
    assert "#thinking" in HTML
    assert "生成中" in HTML
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
    click = _body("onSendClick")
    assert "say()" in click, "空闲时那一枚键不是发送"
    assert "sendBtn.onclick = onSendClick" in _script(), "发送按钮没绑上"
    # 只读壳里不许发：非回环按钮点了只会 403
    assert "if (readonly) { return; }" in say


def test_运行中发送键变成停止而不是禁用() -> None:
    """用户报的第三条：运行中**无法手动中断**。服务端现在有 `POST /run/stop` 了
    （复用 `Runner.stop()` 的温和停止语义），所以那一枚键运行中变成「停止」——
    不再是"禁用 + 一句说明"（那正是原来那个灰键，点了什么也不会发生）。

    证伪法：把 `syncSend` 里的 `mode` 改回只有发送一档（或让 `disabled` 又看 `busy`），
    这条立刻红。
    """
    send = _body("syncSend")
    assert "const mode = busy ? (stopping ? 'stopping' : 'stop') : 'send';" in send
    assert "sendBtn.disabled = stopping || readonly;" in send, (
        "运行中那一枚键又被禁用了：点不动就等于没有停止入口"
    )
    assert "sendBtn.dataset.mode = mode" in send, "没有任何地方说得出它现在是哪一档"
    # 那枚键的说明里必须写清"已产出的内容保留"（措辞可以短，这条事实不行）。
    assert "'停止本轮（已产出的内容保留）'" in send, "运行中那枚键的说明里没写清会丢什么"
    assert "ICON.stop" in send and "stop: '<svg" in _script(), "停止那一档没有自己的图标"

    # 点它：运行中走停止，空闲才走发送；返回值是那一条 POST 的 promise
    # （要能 `await` 到"请求真的发出去了"，而不是只看按钮那一瞬间）。
    click = _body("onSendClick")
    assert "if (busy) { return requestStop(); }" in click
    assert "return say();" in click
    assert "sendBtn.onclick = onSendClick" in _script()

    # 停止走的是新端点，而且**立刻有反馈**（按钮进"正在停止…"）、失败要拨回去。
    stop = _body("requestStop")
    assert "post('/run/stop', {})" in stop
    assert "stopping = true;" in stop and "syncSend()" in stop, "点了没有立刻反馈"
    assert "stopping = false;" in stop, "被拒（409/403）之后那一枚键卡在停止态"
    assert "if (readonly || stopping) { return; }" in stop, "没有防连点"

    # 跑完（或 `/state` 说不在跑）要把那一档落回去，否则会永远写着"正在停止…"。
    run = _body("setRunning")
    assert "if (!busy) { stopping = false; }" in run
    poll = _body("refreshStatus")
    assert "if (!state.running) {" in poll and "if (busy) { setRunning(false); }" in poll, (
        "`final` 事件漏掉时那一枚键会卡在「正在停止…」"
    )
    # 服务端说这一轮**不是**被停掉的（`stopped` 假）= 正常收场：那个标记也得清，
    # 否则下一轮的一次普通失败会被画成"已停止"。
    assert "if (!state.stopped) { stoppedRun = false; }" in _body("applyState")
    final_at = _script().find("data.type === 'final'")
    assert "setRunning(false)" in _script()[final_at : final_at + 200], "跑完没有把那一档放回去"

    # 运行中那句说明改成"点停止就能中断"（不再说"这个壳没有停止接口"）。
    target = _body("drawTarget")
    assert "停止" in target and "手动停止" in target


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
    `state.readonly` 定，而且它一到位就把那一枚键关掉（停止请求也一样：
    `/run/stop` 与 `/run` 走同一张 `WRITE_PATHS` 表，远程一律 403）。
    """
    say = _body("say")
    assert "if (readonly) { return; }" in say

    view = _body("showReadonly")
    assert "state.readonly" in view
    assert "readonly = Boolean(state.readonly)" in view

    send = _body("syncSend")
    assert "readonly" in send
    # 只读与"正在停止"落在同一个禁用条件上（运行中那一档是**可点**的停止键）。
    assert "sendBtn.disabled = stopping || readonly" in send

    stop = _body("requestStop")
    assert "if (readonly || stopping) { return; }" in stop, "只读壳里还发得出停止请求"


def test_只读时审批按钮也不摆出来() -> None:
    body = _body("showActions")
    assert "if (readonly) { return; }" in body
    # 更狠一层：只读壳里连那两个按钮节点都不建（藏起来的空按钮还能被 Tab 到）。
    assert "if (!readonly)" in _body("ensureCard")


# --- ② 待确认：对话流里的卡片 ---


def test_待确认卡片与ChatGPT的调用卡片同形() -> None:
    """卡片上有四样：谁的、哪个文件或哪条命令、diff 摘要、应用 / 拒绝。

    挂点都在类名上（改了类名，那几块就散了）：`.ask-card` / `.ask-who` /
    `.ask-kind` / `.ask-body` / `.ask-actions`。
    """
    for cls in (".ask-card", ".ask-who", ".ask-kind", ".ask-body", ".ask-actions"):
        assert cls in HTML, f"卡片少了 {cls} 这一块"
    card = _body("ensureCard")
    assert "appendNode(node)" in card, "卡片没有挂进对话流"
    assert "'应用'" in card and "'拒绝'" in card
    assert "apply.onclick" in card and "reject.onclick" in card, "按钮写了却没绑上"

    pending = _body("showPending")
    assert "targetLabel()" in pending, "卡片没写清是哪个工作区、哪条会话"
    assert "处改动待确认" in pending
    assert "diffBox(" in pending
    # 同一份待确认内容（补帧 / 重连）只更新不重建：diff 可能有几百行。
    assert "cardSig === sig" in pending

    summary = _body("diffSummary")
    assert "startsWith('+')" in summary and "startsWith('-')" in summary


def test_确认之后卡片变成状态行() -> None:
    """点完不再是横幅、也不再是问句：就地变成"已应用 / 已拒绝"的一行记录。"""
    settle = _body("settleCard")
    assert "'已应用：'" in settle and "'已拒绝：'" in settle
    assert "ask-row" in settle
    assert "card = null;" in settle, "状态行没让出「当前那张卡片」的位置"
    # 下一份待确认另起一张卡片，不是把状态行改回问句。
    assert "if (card) { return card; }" in _body("ensureCard")

    decide = _body("decide")
    assert "post('/confirm', {apply})" in decide
    assert "settleCard(apply ? 'applied' : 'rejected')" in decide
    assert "if (cardApply) { cardApply.disabled = true; }" in decide, "点一次没有禁用"
    # 被拒（409 没有待确认 / 403 只读）：退回"等你点"，别停在已应用上骗人。
    assert "decided = false;" in decide


def test_没有待确认内容时整块UI从页面上拿掉() -> None:
    """`awaiting` 缺了 / 是 0 都按"没有待确认内容"办：**节点也拿掉**。"""
    clear = _body("clearPending")
    assert "removeCard();" in clear, "只是藏起来，DOM 里还占着地方"
    assert "if (!card) { decided = false; return; }" in clear, "缺字段时要能安全早退"
    drop = _body("removeCard")
    assert "removeChild(card)" in drop
    assert "card = null;" in drop
    # 换目标整屏重绑：卡片没了，引用与状态要一起清。
    reset = _body("resetView")
    assert "card = null;" in reset and "cardAsking = false;" in reset

    state = _body("applyState")
    assert "if (state.awaiting) { showActions(); } else { clearPending(); }" in state, (
        "服务端说没有待确认时卡片不消失"
    )


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
