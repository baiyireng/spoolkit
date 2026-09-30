"""网页壳的**行为**层：把内联脚本放进一个假 DOM（`page_dom.js`）里跑。

为什么要有这一层：`test_page.py` 量的是"文本里有没有那个分支"，而这一批要钉住的
是**行为**——"数据没变就一个节点都不重建""历史只追加不重画""点了行立刻亮选中"。
这些在文本层量不出来：把 `renderSidebar` 改回 `replaceChildren()` 全量重画，
文本断言照样全绿（本批改之前就是这样）。

注意：它**证伪不了视觉**——不排版、不算样式，颜色 / 间距 / 滚动条外观只能人眼看。

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


# --- ④ 探活挪到后台之后：横幅要说得出"什么时候探的" ---


def test_探活横幅说得出什么时候探过(dom: dict) -> None:
    """`/state` 给的是**最近一次**探测结果，所以"这是几秒前探的"必须在横幅上。

    去掉 `probedAgo` 那一句，横幅就只剩"连不上"——一个几分钟前的结论看起来
    跟刚刚测的一模一样，那正是"静默的陈旧值"。
    """
    line = dom["health_line"]
    assert "连不上" in line
    assert "3 秒前探过" in line, f"没说这是什么时候探的：{line}"
    assert "spool service" in line, "说了不通却没给下一步"


def test_还没探过不画成通了也不画成断了(dom: dict) -> None:
    """`unknown`（后台探活刚起来）既不是通也不是断，页面上要照实说。"""
    line = dom["health_line_unknown"]
    assert "还没探过" in line
    assert "连不上" not in line


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
