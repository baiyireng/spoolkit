"""网页壳的**行为**层：把内联脚本放进一个假 DOM（`page_dom.js`）里跑。

为什么要有这一层：`test_page.py` 量的是"文本里有没有那个分支"，而这一批要钉住的
是**行为**——"数据没变就一个节点都不重建""历史只追加不重画""点了行立刻亮选中"、
"通知自己会不会消失""没有待确认内容时那块 UI 在不在"。这些在文本层量不出来：
把 `renderSidebar` 改回 `replaceChildren()` 全量重画，文本断言照样全绿（本批
改之前就是这样）。

注意：它**证伪不了视觉**——不排版、不算样式，颜色 / 间距 / 滚动条外观 / 两个气泡
"看上去有没有叠在一起"只能人眼看。它能量的是一条通知排在哪一列里、有没有关闭键、
定时器排了多久、卡片挂在哪个容器下。

node 不在 PATH 就跳过：与 `test_page.py::test_内联脚本能过node语法检查` 同一个态度。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from spoolkit.web.page import HTML

HARNESS = Path(__file__).with_name("page_dom.js")


@pytest.fixture(scope="module")
def dom(tmp_path_factory) -> dict:
    """跑一次假 DOM，把脚本做出来的那些数字一次拿回来。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不在 PATH")
    where = tmp_path_factory.mktemp("page-dom")
    page = where / "page.html"
    page.write_text(HTML, encoding="utf-8")
    done = subprocess.run(
        [node, str(HARNESS), str(page)],
        capture_output=True,
        # 必须显式给 utf-8：这台机器的 locale 是 gbk，而脚本吐的是中文 JSON。
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


# --- ① 切换：只在数据真变了的时候才重绘 ---


def test_同一帧重复到达时一个节点都不重建(dom: dict) -> None:
    """事件流补帧 / 运行状态变化 / 轮询都会重复送同一份状态。

    证伪法：把 `renderSidebar` 改回 `replaceChildren()` 全量重画（或用
    `fillSelect` 每次都 replaceChildren），这条立刻变红——改之前实测：
    同一帧 5 次 = 140 个节点、40 行被替换。
    """
    cost = dom["repeat_frame"]
    assert cost["created"] == 0, f"同一帧重复到达还在建节点：{cost}"
    assert cost["sidebar"] == 0 and cost["rows"] == 0, "侧栏被整段重画了"
    assert cost["stream"] == 0, "消息列表被重画了"
    assert cost["select"] == 0, "两条下拉的备选项被重画了"


def test_点击一行立刻亮起来而不是等回帧(dom: dict) -> None:
    """回帧那一趟里服务端会顺带探活模型，模型不通时要好几秒。

    改之前：点工作区那一列**连请求都不会发**（change 只派发给了会话那条控件），
    而点了之后要等整个回帧才有高亮。现在：点下去同步就亮，且不重建节点。
    """
    assert "ws-b" in dom["optimistic_selected"], "点下去之后选中态没有立刻跟过去"
    assert dom["optimistic_cost"]["created"] == 0, "为了亮一行把节点重建了一遍"


def test_切换完成后服务端补的那一帧不再重画侧栏(dom: dict) -> None:
    """`/target` 成功后服务端还会补一帧 `state`（版本号变了）。

    改之前：那一帧把整段侧栏与两条下拉又重建一遍（实测 16 个节点、5 行）。
    """
    assert dom["switch_duplicate"]["created"] == 0, (
        f"补帧又把侧栏重建了：{dom['switch_duplicate']}"
    )
    # 回帧本身只允许换**真的变了**的那几行：换了工作区，会话那一段整段都变了，
    # 但工作区那两条只是高亮变了——它们不该被重建（签名里不含"亮不亮"）。
    assert dom["switch_reply"]["sidebar"] <= 2, (
        f"回帧重建了太多行：{dom['switch_reply']}"
    )
    assert "ws-b" in dom["switch_selected"]


def test_消息变长了只追加新消息而不是全量重画(dom: dict) -> None:
    """快照里的往来会随运行变长。

    改之前是"画过就不再画"：多出来的那几条**永远不出现**（实测流里新增 0 个节点）。
    现在只补没画过的那一段，已经画好的一条都不重建。
    """
    assert dom["history_added"] == 2, "新到的两条历史没有补进对话流"
    assert dom["history_grew"]["stream"] <= 8, "把整段历史全量重画了一遍"
    assert dom["history_grew"]["sidebar"] == 0, "补历史却把侧栏重建了"


def test_三秒轮询只碰横幅(dom: dict) -> None:
    """`/state` 轮询只喂横幅（配对码 / 探活 / 别处还有谁在跑），不重画列表。"""
    assert dom["poll"]["created"] == 0


# --- ③ 代码块与正文分开 ---


def test_代码块与正文分行画(dom: dict) -> None:
    """改之前 `fillRich` 把 `split('```')` 出来的**每一块**都当代码块画，
    连正文也是——整个气泡看上去全是等宽的，这就是"代码与对话分不开"的根。

    证伪法：把 `fillRich` 改回"每块都 codeBlock"，`prose` 会变成 0。
    """
    rich = dom["rich"]
    assert rich["code_blocks"] == 1, "围栏代码块没有单独画出来"
    assert rich["prose"] == 2, "正文被当成代码块画了（或没画出来）"
    assert rich["lang"] == "powershell", "围栏第一行没有当成语言标签"
    assert rich["code_text"] == "spool web"
    assert "```" not in rich["first_prose_text"], "正文里还留着围栏"
    assert rich["first_prose_text"].endswith("行内代码。")
    assert rich["inline"] == 1, "行内代码没有被单独标出来（与正文没区分）"


# --- ② 连不上模型时说人话 ---


def test_连不上模型时说清哪一步哪一步的地址与下一步(dom: dict) -> None:
    """一行 `LlamaCppError: ConnectTimeout` 谁也读不出下一步该干什么。"""
    parts = dom["failure"]
    assert "连接模型" in parts["head"], "没说清是哪一步失败的"
    assert "http://127.0.0.1:8080" in parts["lines"][0], "没说清连的是哪个地址"
    assert "可能原因" in parts["lines"][1], "没给可能原因"
    assert "spool service list" in parts["lines"][2], "下一步要写**实际存在**的命令"
    assert "spool config" in parts["lines"][2]
    assert "LlamaCppError" in parts["raw"], "原始信息要留着（别把线索藏掉）"
    # 真画出来是一块带左边框的说明（`.sys-line.failure`），不是一行红字。
    assert dom["failure_blocks"] == 1


def test_别的失败不冒充模型故障(dom: dict) -> None:
    """认不出来就走原来的画法：乱指路比不说更坏。"""
    assert dom["failure_unknown"] is None
    # 画出来还是「失败：<原文>」这个老形状，不套模型那一套。
    assert dom["failure_plain"] == "失败：工具 read_file 失败：文件不存在"
    assert dom["failure_plain_blocks"] == 0


def test_只按错误文本也能认出来(dom: dict) -> None:
    """老服务端（或别的失败路径）不给结构化字段时，仍按文本里的词认。"""
    assert dom["failure_by_text"] is True
    assert "按配置里的地址" in dom["address_fallback"]


def test_页面上的错误文本先遮蔽密钥(dom: dict) -> None:
    """密钥、Bearer 串、KEY=VALUE 不许出现在页面上。"""
    assert dom["redact"] == "api_key=*** Bearer *** token=***"


# --- ④ 探活挪到后台之后：通知要说得出"什么时候探的" ---


def test_探活通知说得出什么时候探过(dom: dict) -> None:
    """`/state` 给的是**最近一次**探测结果，所以"这是几秒前探的"必须在通知里。

    去掉 `probedAgo` 那一句，人只会看到一个几分钟前的结论被当成刚刚测的
    ——那正是"静默的陈旧值"。
    """
    toast = dom["health_toast"]
    line = toast["text"]
    assert "连不上" in line
    assert "3 秒前探过" in line, f"没说这是什么时候探的：{line}"
    assert "spool service" in line, "说了不通却没给下一步"
    # 它是右上角那条**可关的浮动通知**（不是横幅）：一条通知一个关闭键。
    assert toast["kind"] == "toast error", f"探活那条不是浮动气泡：{toast['kind']}"
    assert toast["close_buttons"] == len(toast["keys"]), "有通知却没有关闭键"
    # 关闭键里只有一个图标：读屏得有个名字。
    assert toast["close_labels"] == ["关掉这条通知"] * len(toast["keys"])
    assert toast["stack_shown"] is True


def test_同一份探活通知再来一拍不重建节点(dom: dict) -> None:
    """`/state` 是 3 秒一拍：同一份内容重复到达时一个节点都不该新建。"""
    assert dom["health_repeat"]["created"] == 0, (
        f"同一份探活通知被重建了：{dom['health_repeat']}"
    )


def test_模型通了那条通知自己收掉(dom: dict) -> None:
    """状态类的通知（"模型连不上"）不是错误：条件没了就收掉，不用人去关。"""
    assert not any(key.startswith("health|model") for key in dom["health_cleared"]["keys"]), (
        f"模型通了那条通知还挂着：{dom['health_cleared']}"
    )


def test_还没探过不画成通了也不画成断了(dom: dict) -> None:
    """`unknown`（后台探活刚起来）既不是通也不是断，页面上要照实说。"""
    line = dom["health_line_unknown"]
    assert "还没探过" in line
    assert "连不上" not in line


# --- ① 通知：可关的浮动气泡，两类分得清 ---


def test_提示自己会消失而错误不许自己消失(dom: dict) -> None:
    """两类通知的分界：`info` 几秒后自己走；错误**永远不自动消失**
    （人没看到就没了，比不提示更糟），只能手动关。
    """
    info = dom["info_toast"]
    assert 3000 <= info["auto_ms"] <= 8000, f"自动消失的时间不在 3–8 秒里：{info['auto_ms']}"
    assert info["timers"] == 1, "一次性提示没有排自动消失的定时器"
    assert info["close_buttons"] == 1, "一次性提示也要能手动关掉"
    # 到点：那条自己走了，整块也跟着收起来。
    assert info["fired"] == 1
    assert info["after_auto"]["keys"] == []
    assert info["after_auto"]["shown"] is False

    error = dom["error_toast"]
    assert error["new_timers"] == 0, "错误排了自动消失的定时器：人没看到就没了"
    assert error["survived_timers"] is True, "错误自己消失了"
    assert error["kind"] == "toast error"
    assert error["close_buttons"] == 1, "错误没有关闭键，人就只能一直看着它"


def test_失败气泡说清四件事(dom: dict) -> None:
    """失败要说四件事：哪一步 / 哪个地址 / 可能原因 / 下一步敲什么（真存在的命令）。

    四句复用上一轮就有的 `explainFailure`（不是另写一套），原始信息也留着。
    """
    text = dom["error_toast"]["text"]
    assert "失败环节：连接模型" in text, "没说清是哪一步失败的"
    assert "模型地址：http://127.0.0.1:9" in text, "没说清连的是哪个地址"
    assert "可能原因：" in text
    assert "下一步：" in text
    assert "spool service list" in text and "spool config" in text
    assert "ConnectTimeout" in text, "原始信息被藏掉了"


def test_多条通知堆在一起每条都能关(dom: dict) -> None:
    """多条同时出现时排在同一个栈里（一列、互不覆盖），两条以上才给「全部关闭」。"""
    stack = dom["stack"]
    assert len(stack["keys"]) == 2, f"两条在跑却只摆出 {len(stack['keys'])} 条通知"
    assert stack["parents"] == ["toasts", "toasts"], "通知没有挂在同一个栈里"
    assert stack["shown"] is True
    assert stack["all_shown"] is True, "两条以上没有「全部关闭」"
    # 关掉一条：另一条留着，只剩一条时「全部关闭」收起来。
    assert len(stack["after_one"]["keys"]) == 1
    assert stack["after_one"]["shown"] is True
    assert stack["after_one"]["all_shown"] is False
    # 「全部关闭」：一条不剩，整块也跟着收起来。
    assert stack["after_all"]["keys"] == []
    assert stack["after_all"]["shown"] is False


def test_人关掉的那一份不再自己弹回来(dom: dict) -> None:
    """轮询每 3 秒送同一份状态：人关掉之后不许被下一拍又放回来；
    内容真变了（换了个活）才回。
    """
    dismiss = dom["dismiss"]
    assert dismiss["after_close"] == 0
    assert dismiss["same_again"] == 0, "人关掉的那一条被下一拍又弹回来了"
    assert dismiss["changed"] == 1, "内容变了却不回来"


# --- ② 待确认：对话流里的卡片；没有待确认内容时它不存在 ---


def test_没有待确认内容时这块UI在页面上不存在(dom: dict) -> None:
    """不是"显示一个空框"：没有卡片节点、也没有可聚焦的空按钮。"""
    absent = dom["card_absent"]
    assert absent["cards"] == 0
    assert absent["rows"] == 0
    assert absent["apply_buttons"] == 0, "没有待确认内容却摆着「应用」按钮"
    assert absent["reject_buttons"] == 0, "没有待确认内容却摆着「拒绝」按钮"


def test_有待确认时对话流里出现一张卡片(dom: dict) -> None:
    """卡片出现在**对话流里**（不是浮在输入条上方的常驻横幅），
    并说清是谁的、哪个文件、改了多少行。
    """
    card = dom["card_asking"]
    assert card["cards"] == 1
    assert card["in_stream"] == "stream", "卡片没有挂在对话流里"
    assert card["state"] == "asking"
    assert "D:/ws-b" in card["who"] and "a1" in card["who"], f"没写清是谁的：{card['who']}"
    assert "2 处改动待确认" in card["kind"]
    assert any("src/a.py" in one and "+2 -1" in one for one in card["summaries"]), (
        f"没有文件与增删行数：{card['summaries']}"
    )
    assert any("src/b.py" in one and "+1 -0" in one for one in card["summaries"])
    assert card["apply_buttons"] == 1 and card["reject_buttons"] == 1
    # 同一份待确认内容再来一帧（补帧 / 重连）不重建卡片。
    assert dom["card_repeat"]["created"] == 0, f"卡片被重建了：{dom['card_repeat']}"


def test_应用之后卡片变成一行状态(dom: dict) -> None:
    """确认完不在那里当横幅：卡片变成"已应用"的状态行，按钮随之消失。"""
    applied = dom["card_applied"]
    assert applied["calls"] == ["/confirm"], f"没有走 /confirm：{applied['calls']}"
    assert applied["cards"] == 0, "点完之后卡片还留着"
    assert applied["rows"] == 1
    assert applied["text"].startswith("已应用：2 处改动")
    assert "D:/ws-b" in applied["text"] and "a1" in applied["text"]
    assert applied["kind"] == "ask-row ok"
    assert applied["apply_buttons"] == 0 and applied["reject_buttons"] == 0


def test_拒绝是另起一张卡片再变成已拒绝(dom: dict) -> None:
    """上一行的状态行是记录，不因为下一件事又变回问句。"""
    rejected = dom["card_rejected"]
    assert rejected["new_card"] == 1, "新的一份待确认没有另起卡片"
    assert rejected["calls"] == ["/confirm"]
    assert rejected["cards"] == 0
    assert rejected["rows"] == 2, "上一行状态被覆盖掉了"
    assert rejected["last"].startswith("已拒绝：1 处改动")


def test_确认被拒时卡片退回等你点(dom: dict) -> None:
    """服务端说"当前没有待确认的改动"（409）：不许停在"已应用"上骗人。"""
    back = dom["card_reverted"]
    assert back["cards"] == 1, "被拒之后卡片没了"
    assert back["state"] == "asking"
    assert back["apply_disabled"] is False, "被拒之后按钮还是禁用的"


def test_只读壳里那两个按钮根本不建(dom: dict) -> None:
    """只读壳（非回环）下按钮**照样不出现**（既有契约）：点一下被 403 拒是骗人，
    而一个藏起来的空按钮还能被 Tab 到。卡片本身照旧说清在等什么。
    """
    ro = dom["card_readonly"]
    assert ro["cards"] == 1, "只读壳里连「在等什么」都不说了"
    assert ro["state"] == "asking"
    assert ro["apply_buttons"] == 0, "只读壳里摆出了「应用」按钮"
    assert ro["reject_buttons"] == 0, "只读壳里摆出了「拒绝」按钮"
    assert ro["actions"] == 0, "只读壳里连按钮条都建了出来"


def test_命令授权用同一张卡片(dom: dict) -> None:
    """授权和落盘是两件事：一个是问"能不能跑"，一个是问"要不要写入"。
    同一张卡片，写的字不一样（要执行什么 / 为什么）。
    """
    cmd = dom["card_command"]
    assert cmd["kind"] == "命令授权待确认"
    assert "要执行：whoami" in cmd["body"], f"没摆出被问的那条命令：{cmd['body']}"
    assert "原因：白名单外" in cmd["body"]
    assert "grants.json" in cmd["body"], "没给出「要长期允许」的路"
    assert cmd["apply_buttons"] == 1


def test_服务端说没有待确认时卡片从页面上消失(dom: dict) -> None:
    """`awaiting` 变 0 / 字段缺了，都按"没有待确认内容"办：整块 UI 不存在。"""
    gone = dom["card_gone"]
    assert gone["cards"] == 0
    assert gone["apply_buttons"] == 0 and gone["reject_buttons"] == 0
    missing = dom["card_missing_field"]
    assert missing["cards"] == 0, "缺字段时没有按「没有待确认内容」处理"
    assert missing["apply_buttons"] == 0


# --- ③ 历史：显示往来，但不写"给人看的"说明 ---


def test_历史照旧显示只是不加那句说明(dom: dict) -> None:
    """那条"之前的往来（给人看的，不进模型上下文）"是**说给实现者听的**。

    改法：分隔线留着（一条线，不带字），历史消息本身一条都不许少；
    它们多一个 `history` 类（视觉上略淡），这是页面自己认出来的，不新加服务端字段。
    """
    history = dom["history"]
    assert history["seps"] == 1, "分隔线没了（历史与这一趟的界线）"
    assert history["sep_text"] == "", f"分隔线上还写着给人看的那句话：{history['sep_text']}"
    assert history["bubbles"] == 4, "历史消息被删掉了"
    assert history["marked"] == 4, "历史消息没有标记出来（略淡那一档）"
    assert history["texts"] == ["h0", "h1", "h2", "h3"]


# --- ⑤ 新建会话：先调端点，再切目标 ---


def test_点新建会话先建出来再切过去(dom: dict) -> None:
    """点「＋ 新建会话」＝ `POST /session/new` → `POST /target`，而且顺序是这样。

    改了顺序（或退回"只发 /target"）时：前者的服务端还没这条会话，后者压根
    没建——页面上都会看到"请求被拒绝：没有叫 web-new 的会话"。
    """
    made = dom["new_session"]
    assert made["calls"] == ["/session/new", "/target"], (
        f"请求两连发不对：{made['calls']}"
    )
    assert "web-new" in made["selected"], "建完没有切过去（或没点亮那一行）"
