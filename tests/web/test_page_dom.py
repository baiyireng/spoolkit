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
from spoolkit.web.static import bundle_js

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
    bundle = where / "test-bundle.js"
    bundle.write_text(bundle_js(include_test=True), encoding="utf-8")
    done = subprocess.run(
        [node, str(HARNESS), str(page), str(bundle)],
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


def test_归档默认不在左栏只在设置入口露数量(dom: dict) -> None:
    """归档会话不该摊在左栏：默认只露页脚「设置」上的数量，列表要主动打开。"""
    state = dom["archive_default"]
    assert "归档的登录页修复" not in state["sidebar_text"], "归档行又摊回左栏了"
    assert "old-1" not in state["sidebar_text"]
    assert state["count"] == "2"
    assert state["hidden"] is False
    assert state["settings_hidden"] is True, "设置弹层默认就该是关的"


def test_设置里能看到归档会话并恢复(dom: dict) -> None:
    opened = dom["archive_open"]
    assert opened["open"] is True
    assert opened["aria"] == "false"
    assert "归档的登录页修复" in opened["list"]
    assert "old-2" in opened["list"], "没有标题的归档也要能靠 id 找到"
    assert opened["rows"] == 2
    assert opened["restore"] == 2
    assert opened["restore_disabled"] == [False, False]
    assert dom["archive_esc"]["closed"] is True, "Esc 没关掉设置"


def test_当前会话不在会话表里也会补进左栏(dom: dict) -> None:
    """`cli` 这种还没跑过的名字不在 `state.sessions` 里，但它是当前目标。"""
    row = dom["current_session_row"]
    assert row["found"] is True, "当前会话没有补进左栏"
    assert row["highlighted"] is True, "补进来的当前会话没有高亮"
    assert row["subtitle"] == "cli", "次级信息应该是会话 id"


def test_会话行标题优先id次之(dom: dict) -> None:
    assert dom["session_title_sub"] == "a1", "id 没有作为次级信息显示"


def test_种子标题在Agent起名后被替换(dom: dict) -> None:
    title = dom["agent_title"]
    assert title["title"] == "模型起的标题"
    assert title["sub"] == "a1"
    assert title["fetched"] == ["/session/title"]


def test_空闲窗口里又开始跑就不起标题(dom: dict) -> None:
    """final/state 后先等一会儿。这段里已经在跑就放弃，下次空闲还能再问。"""
    found = dom["title_idle"]
    assert 1200 <= found["wait_ms"] <= 1500, found
    assert found["during_wait"] == 0, found
    assert found["after_running"] == 0, found
    assert found["kept"] is True, found
    assert found["after_idle"] == 1, found


def test_点击一行立刻亮起来而不是等回帧(dom: dict) -> None:
    """回帧那一趟里服务端会顺带探活模型，模型不通时要好几秒。

    改之前：点工作区那一列**连请求都不会发**（change 只派发给了会话那条控件），
    而点了之后要等整个回帧才有高亮。现在：点下去同步就亮，且不重建节点。
    """
    assert "ws-b" in dom["optimistic_selected"], "点下去之后选中态没有立刻跟过去"
    assert dom["optimistic_cost"]["created"] == 0, "为了亮一行把节点重建了一遍"
    chosen = dom["optimistic_chosen"]
    assert chosen["root"] == "D:/ws-b", "乐观选中没有写进 store"
    assert chosen["session"] == "a1"


def test_首帧写进store且多个字段同一次通知(dom: dict) -> None:
    assert dom["apply_running"] is False
    assert dom["apply_session"] == "a1"
    assert dom["apply_last_session"] == "a1"
    assert dom["apply_first_keys"] == ["lastState", "session", "chosen"]


def test_输入只通过ctx且Esc收起状态(dom: dict) -> None:
    assert dom["composer_uses_ctx"] is True
    assert dom["escape_closes_status"] is True


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


def test_三秒轮询只碰状态区(dom: dict) -> None:
    """`/state` 轮询只喂状态区（探活 / 模式 / 配对 / 别处还有谁在跑），不重画列表。"""
    assert dom["poll"]["created"] == 0


# --- ① 常驻提示：收敛到右上角，**不许挤对话区** ---


def test_常驻提示出现展开收起都不挤对话区(dom: dict) -> None:
    """用户报的第一条：常驻提示"占满屏幕顶端、压缩会话窗口"。

    假 DOM 不排版，所以这里先把"挤"量化成一条**粗模型**（`page_dom.js` 里的 `room()`）：
    对话区可用高度 = `#thread` 的高 - `#composer` 的高，而 `#composer` 的高按它里面
    在文档流里的**元素**数估（一个节点一行、一行 24px）。判据：四枚徽标全亮 / 详情面板
    展开 / 又回到安静，这个数**一个像素都不许动**，`scrollTop` 与 `scrollHeight`、
    对话流的节点数也一样。

    这条证据本身**可证伪**在两处：
    ① 徽标真的出现了（4 → 1）而面板真的建了节点（反证"什么都没发生"）；
    ② 把五枚徽标搬回输入条那一行——也就是这一轮改掉的那个设计——可用高度当场掉
       288px（`moved_back`），搬回去又原样回来（`restored`）。所以"不变"不是因为
       模型量不出东西。
    """
    squeeze = dom["no_squeeze"]
    quiet = squeeze["quiet"]
    assert quiet["composer_nodes"] == 0, f"输入条那一行里本来就有节点：{quiet}"
    assert quiet["room"] == 528, f"没量到可用高度：{quiet}"
    for name in ("busy", "opened", "clean"):
        assert squeeze[name]["room"] == quiet["room"], (
            f"{name} 那一下对话区可用高度变了：{squeeze[name]} vs {quiet}"
        )
        assert squeeze[name]["composer_nodes"] == quiet["composer_nodes"], (
            f"{name} 那一下往输入条那一行里塞了节点"
        )
        assert squeeze[name]["scrollTop"] == quiet["scrollTop"], f"{name} 挪了滚动位置"
        assert squeeze[name]["scrollHeight"] == quiet["scrollHeight"], f"{name} 改了内容高度"
        assert squeeze[name]["stream_nodes"] == quiet["stream_nodes"], (
            f"{name} 那一下动了对话流的节点"
        )
    # ① 反证：提示真的出现了，而且详情展开是真的建了节点。
    assert squeeze["busy_badges"] == ["st-health"], (
        f"右上角又冒出浮块：{squeeze['busy_badges']}"
    )
    assert "只读" in squeeze["panel_text"] and "其他会话运行中" in squeeze["panel_text"], (
        squeeze["panel_text"]
    )
    assert "482913" in squeeze["panel_text"], squeeze["panel_text"]
    assert squeeze["clean_badges"] == ["st-health"], "收起来之后常态那枚圆点也不见了"
    assert squeeze["panel_nodes"] > 0, "详情面板是空的"
    # ② 反证：搬回输入条那一行就挤，搬回去就不挤——模型量得出"挤"。
    assert squeeze["moved_back"]["room"] < quiet["room"], (
        f"模型量不出挤：这条用例等于没测：{squeeze['moved_back']}"
    )
    assert squeeze["moved_back"]["composer_nodes"] > 0
    assert squeeze["restored"]["room"] == quiet["room"], "搬回去没有复原"
    assert squeeze["restored"]["composer_nodes"] == 0


def test_任务卡默认折叠且有卡时不画旧轨迹(dom: dict) -> None:
    """有 tasks 的助手消息画任务卡；没有 tasks 的旧消息仍画思考过程。"""
    cards = dom["task_cards"]
    assert cards["old_trace"] is True
    assert "第 1 步" in cards["old_trace_text"]
    assert "read_file → 失败" in cards["old_trace_text"]
    assert cards["new_trace"] is False
    assert cards["count"] == 4
    assert cards["arias"] == ["false", "false", "false", "false"]
    assert cards["chevrons"] == 4
    assert cards["chips"] == 12
    assert cards["marks"] == ["验证未过", "失败", "已取消", "完成"]
    assert cards["collapsed"] == "false"
    assert cards["opened_aria"] == "true"
    assert cards["closed_again"] == "false"
    assert "验证未过" in cards["heads"][0]
    assert "写一个文件" in cards["heads"][0]
    assert "2 步" in cards["heads"][0]
    assert cards["goal_title"].startswith("写一个文件")
    assert "而且目标很长" in cards["goal_title"]
    assert len(cards["goal_title"]) > len("写一个文件")
    assert "失败" in cards["heads"][1]
    assert "已达步数上限" not in cards["heads"][1]
    assert "已取消" in cards["heads"][2]
    assert "完成" in cards["heads"][3]
    assert cards["tool"] == "read_file"
    assert cards["intent"] == "读源码"
    assert cards["summary"] == "4 行"
    assert cards["summary_title"] == "4 行"
    assert "task-observe" in cards["summary_parent"]
    assert "task-row-line" not in cards["summary_parent"]
    assert cards["tool_parent"] == "task-row-line"
    assert cards["row_status"] == "完成"
    assert cards["jump"] == "查看改动"
    assert cards["observe_rows"] == 1
    assert cards["approval_rows"] == 0
    assert cards["retrieval_rows"] == 1
    assert cards["verify_rows"] == 1
    assert cards["verify_text"] == "编译失败"
    assert cards["final_rows"] == 1
    assert cards["error_text"] == "已达步数上限"
    assert "预取目录" in cards["opened"]
    assert "结论已生成" in cards["opened"]
    assert "不应该出现在任务卡里" not in cards["opened"]
    assert cards["goal_times"] == 0
    assert cards["raw"] is False
    assert cards["ask_id"] == "tc-1-0"
    assert cards["ask_scrolled"] == 1
    assert cards["ask_focus"] == "ask-apply"
    assert cards["task_apply"] == 0
    running = dom["task_running"]
    assert "assistant" in running["role"]
    assert running["head"].startswith("进行中")
    assert "写一个文件" in running["head"]
    assert "未验证" in running["head"]
    assert running["aria"] == "true"
    assert "search_code" in running["body"]
    assert running["trace_hidden"] is True
    assert running["trace_visible_text"] == ""
    pin = dom["task_pin"]
    assert pin["closed"] == "false"
    assert pin["opened"] == "true"
    assert pin["stayed"] == "true"
    assert pin["opened_summary"] == "又读了一段"
    assert pin["rebound_summary"] == "第三次"
    command = dom["task_command"]
    assert command["collapsed"] == "false"
    assert command["jump"] == "去确认"
    assert command["ask_id"] == "tc-cmd"
    assert "command" in command["ask_cls"]
    assert command["ask_scrolled"] == 1
    assert command["ask_focus"] == "ask-apply"
    assert command["task_apply"] == 0


def test_任务面板列出当前会话并可筛选(dom: dict) -> None:
    """顶栏任务数来自 task_groups。筛选在本地做，详情复用任务卡正文。"""
    panel = dom["task_panel"]
    assert panel["count"] == "4"
    assert panel["wait"] == "1"
    assert panel["closed"] is True
    assert panel["opened"] == "true"
    assert panel["focus"] == "tasks-close"
    assert panel["lines"] == ["还在跑的长目标", "做不完", "验证那件", "先做完"]
    assert panel["marks"] == ["进行中", "失败", "验证未过", "完成"]
    assert panel["wait_mark"] == "待确认"
    assert panel["detail_open"] == "true"
    assert panel["tool"] == "read_file"
    assert panel["jump"] == "查看改动"
    assert panel["raw"] is False
    assert panel["detail_apply"] == 0
    assert panel["ask_id"] == "tc-panel"
    assert panel["ask_scrolled"] == 1
    assert panel["ask_focus"] == "ask-apply"
    assert panel["failed"] == ["做不完", "验证那件"]
    assert panel["done"] == ["先做完"]
    assert panel["awaiting"] == ["还在跑的长目标"]
    assert panel["running"] == ["还在跑的长目标"]
    assert panel["empty"] == "没有匹配的任务"
    assert panel["shrunk"] == "1"
    assert panel["close_focus"] == "tasks-btn"
    assert panel["esc"] is True
    assert panel["esc_focus"] == "tasks-btn"
    assert "待审批" in panel["filters"]
    assert "待审批：" in panel["approval_panel"]


def test_任务搜索只在本地叠在状态筛选上(dom: dict) -> None:
    """目标、工具、摘要、路径都能命中。先按标签再按搜索。Esc 先清搜索再关面板。"""
    found = dom["task_search"]
    assert found["type"] == "search", found
    assert found["label"] == "搜索任务", found
    assert found["placeholder"] == "搜索任务 / 工具 / 文件", found
    assert found["goal"]["lines"] == ["AlphaGoal only"], found["goal"]
    assert found["goal"]["posts"] == 0, found["goal"]
    assert found["tool"]["lines"] == ["藏起来的完成项", "别的目标"], found["tool"]
    assert found["tool"]["posts"] == 0, found["tool"]
    assert found["summary"]["lines"] == ["失败的事"], found["summary"]
    assert found["summary"]["posts"] == 0
    assert found["event_path"]["lines"] == ["路径任务"], found["event_path"]
    assert found["event_path"]["posts"] == 0
    assert found["task_path"]["lines"] == ["路径任务"], found["task_path"]
    assert found["task_path"]["posts"] == 0
    assert found["stacked"]["lines"] == ["别的目标"], found["stacked"]
    assert found["stacked"]["match"] == "共 5 项，匹配 1", found["stacked"]
    assert found["none"]["text"] == "没有匹配的任务", found["none"]
    assert found["none"]["lines"] == [], found["none"]
    assert found["restored"] == ["路径任务", "别的目标"], found["restored"]
    assert found["picked"], found
    assert found["during"]["lines"] == ["别的目标"], found["during"]
    assert found["during"]["id"] == found["picked"], found["during"]
    assert found["during"]["open"] == "true", found["during"]
    assert found["first_esc"]["value"] == "", found["first_esc"]
    assert found["first_esc"]["open"] is True, found["first_esc"]
    assert found["first_esc"]["lines"] == ["路径任务", "别的目标"], found["first_esc"]
    assert found["first_esc"]["id"] == found["picked"], found["first_esc"]
    assert found["second_esc_closed"] is True
    assert found["count"] == "5", found


def test_任务面板接上跨会话历史(dom: dict) -> None:
    """打开时读 /tasks。现场覆盖同 id，当前会话不重复。搜索和筛选不再请求。"""
    found = dom["task_history"]
    assert found["loading"] == "正在加载历史", found
    assert found["loading_lines"] == ["现场目标", "旧的现场"], found
    assert found["loading_posts"] == 1, found
    assert found["during_load_posts"] == 0, found
    assert found["failed"] == "历史加载失败，可重试", found
    assert found["failed_lines"] == ["现场目标", "旧的现场"], found
    assert found["failed_count"] == "2", found
    assert found["retry_posts"] == 1, found
    assert found["current_pressed"] == "true", found
    assert found["current_lines"] == ["现场目标", "旧的现场"], found
    assert found["current_chips"] == 0, found
    assert "历史旧说法" not in found["current_lines"], found
    assert found["scope_posts"] == 0, found
    assert found["all_lines"] == ["现场目标", "别人的新任务", "旧的现场", "别人的旧任务"], found
    assert found["all_chips"] == ["当前这条", "归档标题", "当前这条", "归档标题"], found
    assert found["title"]["lines"] == ["别人的新任务", "别人的旧任务"], found["title"]
    assert found["title"]["posts"] == 0, found["title"]
    assert found["stacked"]["lines"] == ["别人的旧任务"], found["stacked"]
    assert found["stacked"]["match"] == "共 4 项，匹配 1", found["stacked"]
    assert found["stacked"]["count"] == "2", found["stacked"]
    assert found["foreign"]["note"] is True, found["foreign"]
    assert found["foreign"]["jump"] == [], found["foreign"]
    assert found["mine"]["note"] is False, found["mine"]
    assert found["mine"]["jump"] == "查看改动", found["mine"]
    assert found["ready"] == "", found


def test_可见文案不用旧术语(dom: dict) -> None:
    """顶栏、状态面板和任务筛选不再出现旧说法。用户目标正文不在这张清单里。"""
    blobs = [
        dom["health_ok"]["title"],
        dom["health_ok"]["label"],
        dom["health_down"]["label"],
        dom["status_readonly_panel"]["text"],
        dom["status_pinned_panel"]["text"],
        dom["status_pair_panel"]["text"],
        dom["status_elsewhere_panel"]["text"],
        dom["status_no_target_panel"]["text"],
        dom["no_squeeze"]["panel_text"],
        dom["task_panel"]["approval_panel"],
        " ".join(dom["task_panel"]["filters"]),
    ]
    old = (
        "已钉住", "目标未钉住", "等审批", "模型探活", "这一页的运行模式",
        "别处还有一条在跑", "点开查看是哪些",
    )
    for blob in blobs:
        for word in old:
            assert word not in blob, (word, blob)
    assert "目标已固定" in dom["status_pinned_panel"]["text"]
    assert "目标未固定" in dom["status_no_target_panel"]["text"]
    assert "模型连接" in dom["health_ok"]["title"]
    assert "运行模式" in dom["status_readonly_panel"]["text"]


# --- ② AI 回复后消息卡自动更新；思考过程可折叠、事后还能回看 ---


def test_回复到达时占位被真正的回复替换(dom: dict) -> None:
    """用户报的第二条：AI 回复之后卡片不会自动更新（要刷新才看得见）。

    判据是一条**时间线**：`start` 摆出占位「生成中」→ 边跑边攒轨迹（**运行中展开**）
    → 同 phase wait 替换末行 → `final` 到达时同一张卡片占位换成回复并收起思考过程。
    整条时间线里 `.bubble` 只该有一个：不许留下占位、也不许另起一张。
    """
    placeholder = dom["reply_placeholder"]
    assert placeholder["pending"] is True, "start 之后没有摆出占位卡片"
    # 占位是**名词性**的状态词（不是拟人的"正在思考"）：它说的是"这一轮在生成"。
    assert placeholder["bubble_text"] == "生成中", f"占位不是那句话：{placeholder}"
    assert placeholder["bubbles"] == 1
    assert placeholder["trace_nodes"] == 0, "start 时轨迹还该是收起的"
    assert placeholder["trace_aria"] == "false"

    live = dom["reply_live"]
    # step + tool + 一条 wait（第二次同 phase wait 替换了第一次）→ 3 行标题。
    assert live["trace_head"].startswith("思考过程 · 3 行"), (
        f"轨迹标题没有说清几行：{live['trace_head']}"
    )
    assert "有失败" in live["trace_head"]
    assert live["trace_aria"] == "true", "运行中思考过程应自动展开"
    # 3 行轨迹；失败工具带 detail 子节点，所以 DOM 节点数是 4。
    assert live["trace_nodes"] == 4, f"运行中应能看见轨迹节点：{live['trace_nodes']}"
    assert dom["reply_live_bubbles"] == 2, "边跑边攒轨迹时又多出了气泡"

    opened = dom["reply_open"]
    assert opened["trace_aria"] == "true"
    assert "第 1 步" in opened["trace_text"]
    assert "read_file → 失败" in opened["trace_text"], "工具那一行没有说清成功还是失败"
    assert "文件不存在" in opened["trace_text"], "失败那行没有带上原因（排错要的就是它）"
    assert "已等待 27s" in opened["trace_text"], "同 phase wait 应显示更新后的秒数"
    assert "已等待 12s" not in opened["trace_text"], "旧 wait 行不该还在"

    phased = dom["reply_wait_phases"]
    assert "输出加长" in phased["trace_text"]
    assert "思考过程 · 4 行" in phased["trace_head"], phased["trace_head"]

    done = dom["reply_done"]
    assert done["bubble_text"] == "改好了：加了校验", f"回复没有替换掉占位：{done}"
    assert done["pending"] is False, "气泡上还挂着「占位」那个类"
    assert done["bubbles"] == 1, "回复另起了一张卡片（占位那张还在）"
    assert done["trace_aria"] == "false", "成功收尾后思考过程应收起"
    assert done["trace_nodes"] == 0, "收起后正文节点不该占着"


def test_库里那条回复到达时不新建节点也不重复(dom: dict) -> None:
    """`/state` 每 3 秒一拍、跑完还会补一帧：同一句回复会被送**两次**
    （`final` 事件一次、记忆库里那条一次）。第二次必须**认领**同一张卡片，
    不能又画一个气泡出来、也不能把正文叠两遍。
    """
    persisted = dom["reply_persisted"]
    # 两条：我说的那条 + 它答的那条；而**认领之后总数还是 2**（没有新建节点）。
    assert dom["reply_done_bubbles"] == 2, "回复替换占位时多出了气泡"
    assert persisted["stream_bubbles"] == 2, (
        f"对话流里应该正好两条（我说的 + 它答的）：{persisted['texts']}"
    )
    assert persisted["texts"] == ["改一下登录页", "改好了：加了校验"], (
        f"顺序或内容不对（重复画 / 少画）：{persisted['texts']}"
    )
    assert persisted["bubbles"] == 1, "同一张卡片里被填了两遍"
    assert persisted["bubble_text"] == "改好了：加了校验", "正文叠了一遍"
    # 「生成中」这句占位不许留在**气泡**里（`has_placeholder` 是整条对话流的文字，
    # 而等待心跳那行也带"生成中"三个字——所以这里钉的是气泡那一处）。
    assert persisted["bubble_placeholder"] is False, "气泡里还留着「生成中」那句占位"


def test_刷新之后思考过程与失败原因依然回看得见(dom: dict) -> None:
    """这一批最硬的那条要求：**刷新页面 / 切走再切回，仍能回看那一轮的思考与失败原因**。

    假 DOM 里的"刷新"就是 `resetView()`（对话流清空、这一轮的实时状态全部作废），
    然后只喂一帧 `/state`——那一帧里的助手消息带着服务端只增的 `trace`。
    判据：卡片上有一个**默认收起**的「思考过程」；点开后步骤、工具调用、失败那行
    （带原因）都在；而这一轮的失败原因与**下一步**（`spool service list` 这类真命令）
    直接画在卡片里——不必点开也看得见。
    """
    fresh = dom["refresh_trace"]
    assert fresh["has_placeholder"] is False, "刷新之后还挂着占位"
    assert fresh["trace_head"] == "思考过程 · 3 行 · 有失败", fresh["trace_head"]
    assert fresh["trace_nodes"] == 0, "默认该是收起的"
    assert "失败环节：连接模型" in fresh["card_text"], "失败原因没有重新画出来"
    assert "下一步：" in fresh["card_text"], "没有下一步"
    assert "spool service list" in fresh["card_text"], "下一步要写**实际存在**的命令"
    assert "可能原因：" in fresh["card_text"]

    opened = dom["refresh_trace_open"]
    assert opened["trace_aria"] == "true"
    assert "第 1 步" in opened["trace_text"], "步骤没有回放出来"
    assert "read_file → 失败" in opened["trace_text"], "工具调用没有回放出来"
    assert "文件不存在" in opened["trace_text"], "失败原因没有回放出来"
    assert "失败：没做成：文件不存在" in opened["trace_text"], "结局那行没有回放出来"


# --- ⑤ 运行中那一枚键是「停止」 ---


def test_运行中发送键变停止点了就调那个端点(dom: dict) -> None:
    """用户报的第三条：无法手动中断任务。

    判据：运行中那一枚键是"停止"（可点，不是禁用），说明里写清"已产出的内容保留"；
    点它发的就是 `POST /run/stop`，而且**立刻**进"正在停止…"；
    `/state` 说它已经不在跑之后，那一枚键回到"发送"。
    """
    button = dom["stop_button"]
    assert button["mode"] == "stop", f"运行中那一枚键不是停止：{button}"
    assert button["disabled"] is False, "运行中那一枚键是禁用的：点不动等于没有停止入口"
    assert "停止" in button["title"] and "保留" in button["title"], button["title"]

    clicked = dom["stop_clicked"]
    assert clicked["calls"] == ["/run/stop"], f"没有调那个端点：{clicked['calls']}"
    assert clicked["mode"] == "stopping", "点了没有立刻反馈"
    assert clicked["disabled"] is True, "正在停止时还能再点（会重复发请求）"
    # 停止请求已发出：按钮上是**停止中**（不是"正在停止…"那种旁白口气）。
    assert "停止中" in clicked["title"], clicked["title"]
    assert clicked["last_line"] is True, "点了之后对话区里没有一句回执"

    settled = dom["stop_settled"]
    assert settled["mode"] == "send", f"停下来之后那一枚键没回到发送：{settled}"
    assert settled["disabled"] is False
    assert settled["title"] == "发送"


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
    """老服务端（或别的失败路径）不给结构化字段时，仍按文本里的词认。

    没有地址可退时那一行要**照实说**"地址没给"（不是留空、也不是编一个）。
    """
    assert dom["failure_by_text"] is True
    assert "未提供" in dom["address_fallback"], dom["address_fallback"]


def test_页面上的错误文本先遮蔽密钥(dom: dict) -> None:
    """密钥、Bearer 串、KEY=VALUE 不许出现在页面上。"""
    assert dom["redact"] == "api_key=*** Bearer *** token=***"


# --- ④ 探活搬进状态区（旧的"探活通知"那一组换成了上面那几条） ---


def test_同一份探活再来一拍不重建节点(dom: dict) -> None:
    """`/state` 是 3 秒一拍：同一份内容重复到达时一个节点都不该新建。"""
    assert dom["health_repeat"]["created"] == 0, (
        f"同一份探活把节点重建了：{dom['health_repeat']}"
    )


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
    cap = dom["toast_cap"]
    assert cap["count"] == 3, f"通知超过 3 条：{cap}"
    assert cap["newest"] == "failure|cap|四", f"最新一条不在栈顶：{cap}"
    assert "failure|cap|一" not in cap["keys"]
    assert cap["alert"] == "alert"


def test_人关掉的那一份不再自己弹回来(dom: dict) -> None:
    """轮询每 3 秒送同一份状态：人关掉之后不许被下一拍又放回来；
    内容真变了（换了个活）才回。
    """
    dismiss = dom["dismiss"]
    assert dismiss["after_close"] == 0
    assert dismiss["same_again"] == 0, "人关掉的那一条被下一拍又弹回来了"
    assert dismiss["changed"] == 1, "内容变了却不回来"


# --- ④ 探活搬进状态区：常态只剩一枚圆点，异常变色带字，点开有详情 ---


def test_探活正常时只剩一枚圆点(dom: dict) -> None:
    """常态下**不画任何提示**（不是"显示一句一切正常"）：徽标上只有圆点与空标签，
    而且探活**一条气泡都不产生**（它不再是通知）。

    证伪法：把 `healthView` 常态的 `label` 改回一句"模型正常"，或让 `showHealth`
    再走 `showToast`，这条立刻红。
    """
    ok = dom["health_ok"]
    assert ok["label"] == "正常", f"常态入口不是「正常」：{ok}"
    assert "ok" in ok["cls"] and "bad" not in ok["cls"], f"常态不是低调那档：{ok['cls']}"
    assert ok["visible"] == ["st-health"], f"常态下多出了别的徽标：{ok['visible']}"
    assert ok["parts"] == 2, "常态那枚徽标就只有圆点 + 空标签两块"
    assert ok["keys"] == [], f"探活还弹气泡：{ok['keys']}"
    assert "模型连接：正常" in ok["title"], "状态入口悬停/读屏说不出它是什么"


def test_探活异常时变色带字而不是一枚沉默的绿点(dom: dict) -> None:
    """"异常必须仍然看得见"：未连接时徽标是红的并且**带字**（可判定的状态词，
    不是一枚沉默的绿点）；"未探测"是黄的（既不是通也不是断）。

    证伪法：把 `showHealth` 里的 `label` 去掉（只留变色），这条红。
    """
    down = dom["health_down"]
    assert down["label"] == "未连接", f"连接失败没有落成「未连接」：{down}"
    assert "warn" in down["cls"], f"未连接没有变色：{down}"
    assert down["visible"] == ["st-health"], "异常时冒出了别的徽标"
    # 变色那一下不该重建节点（`/state` 是 3 秒一拍，重建就是每 3 秒抖一次）。
    assert down["cost"]["created"] == 0, f"改个颜色重建了节点：{down['cost']}"
    assert down["panel_open"] is False, "详情默认就是开着的"
    assert dom["health_repeat"]["created"] == 0, "同一份探活被重建了"

    unknown = dom["health_unknown"]
    assert unknown["label"] == "未连接" and "warn" in unknown["cls"]

    # 模型又通了：回到常态，而且没有留下任何气泡。
    back = dom["health_back"]
    assert back["label"] == "正常" and back["keys"] == [], f"通了之后还留着东西：{back}"


def test_探活详情保留状态地址探活时间与下一步(dom: dict) -> None:
    """详情里要有：状态 / 地址 / 探活时间（`probed_at` + `age_s`）/ 可能原因 / 下一步。

    钉的是**要点**：状态词可判定、地址在、"什么时候探的"（时刻 + 陈旧度）在、
    原因与下一步接进来了。探活挪到后台之后 `/state` 给的是最近一次结果，
    **静默的**陈旧值比报错更糟。
    """
    panel = dom["health_panel"]
    assert panel["open"] is True
    text = panel["text"]
    assert "状态：未连接" in text, f"详情里没有状态：{text}"
    assert "模型服务地址：http://127.0.0.1:8080" in text, f"详情里没有地址：{text}"
    assert "最近一次探测：" in text, f"详情里没有探活时刻：{text}"
    assert "3 秒前" in text, f"没说这是什么时候探的：{text}"
    assert "可能原因：" in text, f"详情里没有可能原因：{text}"
    assert "下一步：" in text and "spool service list" in text, f"没有下一步的命令：{text}"
    assert "拒绝连接" in text, "原始信息被藏掉了"
    # 展开详情**不动对话流**：那些新节点全在面板下（面板是绝对定位的浮层）。
    assert panel["panel_nodes"] > 0, "详情面板是空的"
    assert panel["panel_root"] == "st-panel", "详情节点挂到别人家去了"
    assert panel["panel_first"] == "st-close", "关闭键不在详情的 DOM 首位"
    assert panel["stream_nodes"] == panel["stream_nodes_before"], "展开详情把对话流重画了"
    assert panel["stream_children"] == panel["stream_children_before"]


def test_状态详情能收起来(dom: dict) -> None:
    """收起之后面板不在页面上，而且按钮上的 `aria-expanded` 跟着走
    （读屏要问得出来"现在开着没有"）。"""
    assert dom["health_panel"]["aria"] == "true"
    closed = dom["health_panel_closed"]
    assert closed["open"] is False
    assert closed["aria"] == "false"
    assert dom["status_clean"]["panel"] is False, "默认就该是收起的"


def test_点外面或按Esc都能收起详情(dom: dict) -> None:
    """焦点不一定在输入框里：收起详情不能只挂在 `goalBox.onkeydown` 那条上。

    证伪法：把 `document.onkeydown` / `document.onclick` 那两处删掉，这条红。
    """
    assert dom["status_esc"]["open"] is False, "Esc 收不掉（焦点在徽标上时就是这样）"
    assert dom["status_outside"]["open"] is False, "点 composer 之外收不掉"
    assert dom["status_inside"]["open"] is True, "点徽标自己那一下把面板关了"


def test_常态下状态区只看得见一枚圆点(dom: dict) -> None:
    """只读 / 钉住 / 待配对 / 别处在跑**都不是常态**：它们没有的时候，那一行里
    只剩探活那枚小圆点——"顶部干净"在输入条这一行的样子。"""
    assert dom["status_clean"]["visible"] == ["st-health"], (
        f"常态下多出了徽标：{dom['status_clean']['visible']}"
    )
    assert dom["health_ok"]["visible"] == ["st-health"]


def test_只读与钉住收进模式徽标并说得出为什么点不动(dom: dict) -> None:
    """只读（非回环）与 `--root` 钉住回答的是"为什么点不动"，所以**不许变得不可发现**：
    徽标悬停就有标题、点开是全文。"""
    ro = dom["status_readonly"]
    assert "st-mode" not in ro["visible"]
    panel = dom["status_readonly_panel"]["text"]
    assert "只读" in panel and "写请求会被服务端拒" in panel and "--host" in panel, panel

    pinned = dom["status_pinned"]
    assert "st-mode" not in pinned["visible"]
    assert "目标已固定" in dom["status_pinned_panel"]["text"]
    assert "--root" in dom["status_pinned_panel"]["text"]

    assert "st-mode" not in dom["status_clean"]["visible"]


def test_待配对与别处在跑收进状态区但都不丢(dom: dict) -> None:
    """配对码与"别处还有一条在跑"都收进同一行了，但**都不许变得不可发现**：
    配对码直接写在徽标上（人要去终端敲它），别处那条点开看是哪几条。"""
    pair = dom["status_pair"]
    assert "st-pair" not in pair["visible"]
    text = dom["status_pair_panel"]["text"]
    assert "spool approve 482913" in text and "来自：u-1" in text, text
    assert "不批准配对" in text, "没说清那两个按钮不批配对"

    elsewhere = dom["status_elsewhere"]
    assert "st-elsewhere" not in elsewhere["visible"]
    et = dom["status_elsewhere_panel"]["text"]
    assert "ws-c" in et and "其他会话运行中" in et, et
    assert "等待确认" in et, et

    # 都收掉之后那一行又只剩圆点。
    assert dom["status_clean"]["visible"] == ["st-health"]


def test_没有目标时那枚徽标仍然指得动路(dom: dict) -> None:
    """空态引导从顶部横幅搬到了「未选工作区」徽标 + 详情：那句话一个字都没少。

    钉要点：空态**说得出下一步怎么选**（去哪一段选、表空的怎么办），
    而不是只摆一个"没目标"。
    """
    empty = dom["status_no_target"]
    assert "st-target" not in empty["visible"]
    text = dom["status_no_target_panel"]["text"]
    assert "未选择工作区" in text and "工作区" in text, text
    assert "spool init" in text or "选一项" in text, f"空态没给出路：{text}"


# --- ① 通知**不许把页面挤下去** ---


def test_弹一条通知不动对话流也不动滚动位置(dom: dict) -> None:
    """假 DOM 不排版，但"被挤下去"在 DOM 层就是两件事：往对话流里塞了节点、
    或者动了主滚动容器的滚动位置 / 内容高度。弹一条通知前后这两样都得一模一样。

    证伪法：把 `showToast` 里那个 `toastsBox.appendChild(node)` 改成
    `appendNode(node)`（挂进对话流），`stream_children` / `stream_nodes` 立刻变，
    这条红。
    """
    push = dom["toast_no_push"]
    assert push["before"]["stream_nodes"] > 0, "这条用例没量到对话流里的东西（测了个寂寞）"
    assert push["before"]["scrollTop"] == 120, "没量到真的滚动位置"
    assert push["after"] == push["before"], f"弹一条通知把页面动了：{push}"
    assert push["after_close"] == push["before"], f"关掉那条通知又动了一次：{push}"
    # 反证：气泡确实建出来了，而且它挂在 `#toasts` 下——**不在对话流里**。
    assert push["parent_id"] == "toasts", f"气泡挂到别处去了：{push}"
    assert push["in_stream"] == "toasts", "通知被挂进对话流了"
    assert push["container_root"] == "toasts"


# --- ③ 自己说的话不带符号前缀 ---


def test_自己说的话不带符号前缀(dom: dict) -> None:
    """发出去的那一条就是原话：气泡底色 + 右对齐已经说清"这是我发的"。

    证伪法：把 `say()` 改回 `add('▶ ' + goal, 'me')`，这条红。
    """
    said = dom["say_text"]
    assert said["calls"] == ["/run"], f"没有真的走 /run：{said['calls']}"
    assert said["last_user"] == "改一下登录页", f"气泡里多了别的东西：{said['last_user']!r}"


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
    assert card["cls"] == "ask-card"
    assert card["apply_label"] == "应用全部改动"
    assert card["reject_label"] == "拒绝全部"
    assert card["apply_buttons"] == 1 and card["reject_buttons"] == 1
    assert len(card["folds"]) == 2
    assert all(one["expanded"] == "true" and one["open"] is True for one in card["folds"])
    # 同一份待确认内容再来一帧（补帧 / 重连）不重建卡片。
    assert dom["card_repeat"]["created"] == 0, f"卡片被重建了：{dom['card_repeat']}"


def test_应用之后卡片变成一行状态(dom: dict) -> None:
    """确认完不在那里当横幅：卡片变成"已应用"的状态行，按钮随之消失。"""
    applied = dom["card_applied"]
    assert applied["calls"] == ["/confirm"], f"没有走 /confirm：{applied['calls']}"
    assert applied["body"] == '{"apply":true}'
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
    assert rejected["body"] == '{"apply":false}'
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


def test_命令授权卡与文件改动卡分开(dom: dict) -> None:
    """授权和落盘是两件事：命令卡用警告样式和「允许执行 / 拒绝」，
    文件卡用「应用全部改动 / 拒绝全部」。点完的状态行也按这件事分开写。
    """
    cmd = dom["card_command"]
    assert cmd["cls"] == "ask-card command"
    assert cmd["kind"] == "命令授权待确认"
    assert "要执行一条命令" in cmd["title"]
    assert cmd["pre"] == "PRE" and cmd["code"] == "whoami"
    assert cmd["reason"] == "原因：白名单外"
    assert "仅允许这一次" in cmd["warn"] and "grants.json" in cmd["warn"]
    assert "应用" not in cmd["card_text"], f"命令卡上还写着应用：{cmd['card_text']}"
    assert cmd["allow"] == 1 and cmd["deny"] == 1
    assert cmd["file_apply"] == 0 and cmd["file_reject"] == 0
    allowed = dom["card_command_allowed"]
    assert allowed["body"] == '{"apply":true}'
    assert allowed["text"].startswith("已允许执行：命令 whoami")
    denied = dom["card_command_denied"]
    assert denied["body"] == '{"apply":false}'
    assert denied["text"].startswith("已拒绝：命令 echo denied")


def test_大diff默认收起并且文件头能折叠(dom: dict) -> None:
    """超过 40 行的 diff 先收起，小的先展开。点文件头切换 aria-expanded；
    同一份内容再来一帧不重建，所以刚展开的状态还在。
    """
    fold = dom["diff_fold"]
    assert fold["files"] == 2
    assert fold["big_lines"] == 200
    assert fold["big_before"] == "false" and fold["big_after"] == "true"
    assert fold["big_open_after"] is True
    assert fold["big_body"] == "diff-file-body"
    assert fold["small_before"] == "true" and fold["small_after"] == "false"
    assert fold["small_open_after"] is False
    assert fold["repeat"]["created"] == 0, f"折叠状态被重建冲掉了：{fold['repeat']}"
    assert fold["still"] == "true"


def test_同一帧的改动和命令一起确认(dom: dict) -> None:
    """同一帧里 diff 和命令都在时，不能先画文件再被命令盖掉。
    一张组合卡同时摆出两边，点一次只发一次 /confirm。
    """
    both = dom["card_both"]
    assert both["cls"] == "ask-card combined"
    assert both["kind"] == "2 处改动 + 1 条命令待确认"
    assert both["files"] == 2 and both["cmds"] == 1
    assert both["code"] == "git status"
    assert both["reason"] == "原因：要看工作区"
    assert "仅允许这一次" in both["warn"] and "grants.json" in both["warn"]
    assert both["apply_label"] == "应用改动并允许执行"
    assert both["reject_label"] == "全部拒绝"
    assert both["file_apply"] == 0 and both["allow"] == 0
    assert both["expanded_before"] == "true" and both["expanded_flipped"] == "false"
    assert both["repeat"]["created"] == 0, f"组合卡被重建了：{both['repeat']}"
    assert both["still"] == "false"
    assert both["posts"] == [{"url": "/confirm", "body": '{"apply":true}'}]
    assert both["text"].startswith("已应用 2 处改动并允许执行：命令 git status")
    denied = dom["card_both_denied"]
    assert denied["posts"] == [{"url": "/confirm", "body": '{"apply":false}'}]
    assert denied["text"].startswith("已拒绝：2 处改动 + 命令 git status")


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
    assert made["selected_sub"] == "web-new", "建完没有切过去（或没点亮那一行）"
    assert "未命名会话" in made["selected"], "新会话没标题时应显示可读占位名"


# --- ⑥ 收尾报告的**分档**：结论段干净、详情与用量进「思考过程」 ---
#
# 现场是用户在网页壳里真实看到的那一屏：`tests/fixtures/interact_probe/web-real-reports.md`
# （第二次运行，20261006-022747-299732）。那一屏的结论段里混着「本次运行共落成 1 处
# 改动 / 最终文件状态 / 指纹」与「用量：…」，过程区里还混进了一句系统对模型说的话。
# 下面这几条钉的是**渲染档位**（正文一个字不删，只是换个地方摆）。


def test_结论段里没有内部账与用量(dom: dict) -> None:
    """气泡里只留「做成了什么 + 验没验 + 我能做什么」。

    证伪法：把 `fillReport` 退回 `fillReply(card, text)`（整份报告进气泡），
    下面每一条都会红——这正是改之前的那一屏。
    """
    card = dom["report_tiers"]
    bubble = card["bubble_text"]
    assert "已创建文件 hello.txt，内容为 Hello Word。" in bubble
    assert "没有校验器（不是失败）。" in bubble
    assert "撤销这一轮：spool revert --run" in bubble
    for banned in ("详情：", "指纹", "共落成", "最终文件状态", "用量："):
        assert banned not in bubble, f"结论段里还混着内部账/内部量：{banned}——{bubble}"


def test_详情与用量都进了思考过程(dom: dict) -> None:
    """详情**没有被删掉**，只是折进了那块默认收起的轨迹里（信息一个字不丢）。"""
    collapsed = dom["report_tiers"]
    # 收起时正文块一个节点都不建——详情行照样算进了"几行"里。
    assert collapsed["trace_nodes"] == 0, "轨迹默认就该是收起的"
    assert "5 行" in collapsed["trace_head"], collapsed["trace_head"]

    opened = dom["report_tiers_open"]
    assert "详情：本次运行共落成 1 处改动" in opened["trace_text"], opened["trace_text"]
    assert "指纹 44d3a83db52a/1行" in opened["trace_text"]
    assert "用量：3 步 · 3 次调用 · 13025+207 token" in opened["trace_text"]
    # 用量是在结局**之后**到的：它挂回了刚画好的那张卡片，没有另起一张空卡片。
    assert collapsed["cards"] == 1, "用量另起了一张卡片（页面上多出一张幽灵回复卡）"
    assert collapsed["cards_before_usage"] == 1, collapsed["cards_before_usage"]


def test_第三档在过程区说跳过而不是成功(dom: dict) -> None:
    """`ok=true` 只是「没有发现问题」，不是「验过了」（问题⑤）。

    状态字与结论段那句**刻意不同**：结论段回答"验没验"，过程区这一行回答
    "这一步真的跑了吗"。同一句话在两处出现两次，正是这一轮修掉的残渣。
    """
    opened = dom["report_tiers_open"]
    assert "最终验证 → 跳过（不是失败）" in opened["trace_text"], opened["trace_text"]
    assert "最终验证 → 成功" not in opened["trace_text"], "第三档冒充了「成功」"
    assert "这次改的是 .txt。" in opened["trace_text"], "第三档没有给出理由"
    assert "没有校验器（不是失败）" not in opened["trace_text"], (
        "过程区把结论段那一句又说了一遍"
    )


def test_刷新之后报告的分档一样(dom: dict) -> None:
    """库里存的仍是**完整那份报告**：认领与重画走同一条 `fillReport`，
    所以刷新 / 切走再切回之后，结论段依旧是干净的那一段。"""
    card = dom["report_tiers_history"]
    assert "已创建文件 hello.txt" in card["bubble_text"]
    assert "详情：" not in card["bubble_text"], card["bubble_text"]
    assert "详情：本次运行共落成 1 处改动" in card["trace_text"], card["trace_text"]


def test_键盘能切目标并且抽屉Esc回到菜单(dom: dict) -> None:
    """可见行是按钮：空格走同一条 /target；抽屉打开后焦点进去，Esc 回到菜单。"""
    row = dom["row_key"]
    assert row["role"] == "button"
    assert row["tab"] == 0
    entered = dom["row_enter"]
    assert entered["posts"] == ["/target"], entered
    assert entered["chosen"]["root"], entered
    opened = dom["drawer_open"]
    assert opened["drawer"] is True
    assert opened["expanded"] == "true"
    assert opened["focus"] == "new-session", opened
    closed = dom["drawer_esc"]
    assert closed["drawer"] is False
    assert closed["expanded"] == "false"
    assert closed["focus"] == "menu", closed
    assert dom["no_hidden_select"] is True
    assert dom["panel_close"]["label"] == "关闭状态详情"
    assert dom["panel_close"]["click"] is True
    live = dom["live"]
    assert live["conn_role"] == "alert" and live["conn"] == "assertive"
    assert live["thinking"] == "status"
    assert live["toasts"] == "polite"
    assert live["goal"] == "goal-label"
    assert live["stream"] == "log"


def test_草稿按会话隔离并且新建不继承(dom: dict) -> None:
    """A 的半句不能出现在 B；同一目标的空帧也不能把正在打的字盖掉。"""
    drafts = dom["drafts"]
    assert drafts["blank_b"] == "", drafts
    assert drafts["back_a"] == "A 的一半", drafts
    assert drafts["back_b"] == "B 的另一半", drafts
    assert drafts["kept_typing"] == "还在打", drafts
    assert drafts["map_a"] == "A 的一半", drafts
    assert drafts["map_b"] == "B 的另一半", drafts
    assert drafts["fresh_value"] == "", drafts
    assert drafts["fresh_draft"] is False, drafts
    assert drafts["restored_a"] == "A 的一半", drafts
    assert "/session/new" in drafts["new_posts"], drafts
    assert "/target" in drafts["new_posts"], drafts


def test_发送失败恢复草稿并且重试不重复气泡(dom: dict) -> None:
    """500 和网络异常都把原文放回输入框；重试成功只发一次，也不再画第二条。"""
    failed = dom["send_500"]
    assert failed["value"] == "五百的原文", failed
    assert failed["focus"] == "goal", failed
    assert failed["draft"] == "五百的原文", failed
    assert failed["bubbles"] == 1, failed
    assert failed["users"] == 1, failed
    assert "未发送" in failed["unsent"], failed
    assert failed["retry"] == "重试", failed
    assert failed["pending"] == "", failed
    trip = dom["send_500_roundtrip"]
    assert trip["away"] == "", trip
    assert trip["back"] == "五百的原文", trip

    net = dom["send_net"]
    assert net["rejected"] is False, net
    assert net["value"] == "断网的原文", net
    assert net["focus"] == "goal", net
    assert net["draft"] == "断网的原文", net
    assert net["bubbles"] == 1 and net["retry"] == "重试", net
    assert net["pending"] == "", net
    assert net["users_before"] == 1 and net["users_after"] == 1, net
    assert net["still_failed"] == 1, net
    assert net["posts"] == ["/run"], net
    assert net["notes"] == net["notes_before"], net

    retry = dom["send_retry"]
    assert retry["before"]["posts"] == ["/run"], retry
    assert retry["before"]["users"] == 1, retry
    assert retry["before"]["failed"] == 0, retry
    assert retry["before"]["pending"] == "重试成功的原文", retry
    assert retry["before"]["value"] == "", retry
    assert retry["users"] == 1, retry
    assert retry["pending"] == "", retry


def test_标题列表表格和行内代码一起画出来(dom: dict) -> None:
    """块级标记和行内代码在同一段里：标题、列表、表格、粗体、链接都要成节点。"""
    md = dom["markdown"]
    assert md["heading"] == "结论", md
    assert md["items"] == ["先跑 spool status", "注意 看 文档"], md
    assert md["codes"] == ["spool status", "a"], md
    assert md["strong"] == "注意", md
    assert md["link"] == "https://example.test/docs", md
    assert md["cells"] == ["a", "b"], md
    assert "##" not in md["raw"] and "**" not in md["raw"] and "`" not in md["raw"], md


def test_空态按目标和历史重算(dom: dict) -> None:
    """没选目标、选了但还没说话，是两种空态。示例只填输入框。"""
    pick = dom["empty_pick"]
    assert pick["mode"] == "pick", pick
    assert "先选一个工作区和会话" in pick["text"], pick
    assert pick["go"] == "去选择工作区", pick
    assert pick["drawer"] is True, pick
    ready = dom["empty_ready"]
    assert ready["mode"] == "ready", ready
    assert "ws-a" in ready["text"] and "A 的第 1 条" in ready["text"], ready
    assert "让 Agent 做点什么" in ready["text"], ready
    assert ready["samples"] == ["解释这个项目结构", "跑测试并修失败", "找我最近改过的文件"], ready
    assert ready["goal"] == "解释这个项目结构", ready
    assert ready["focus"] == "goal", ready
    assert ready["posts"] == 0, ready
    assert ready["draft"] == "解释这个项目结构", ready
    assert dom["empty_after_send"]["shown"] == 0, dom["empty_after_send"]
    switched = dom["empty_switched"]
    assert switched["shown"] == 1, switched
    assert switched["mode"] == "ready", switched
    assert "A 的第 2 条" in switched["text"], switched
    assert "A 的第 1 条" not in switched["text"], switched


def test_顶栏是工作区名和会话标题(dom: dict) -> None:
    """长路径不进顶栏正文。完整根在 title 和状态详情里。没目标仍是原来那句。"""
    bar = dom["where_bar"]
    assert bar["text"] == "beta · 当前会话", bar
    assert "D:/" not in bar["text"], bar
    assert bar["title"] == "D:/very/long/path/that/should/not/wrap/beta-workspace", bar
    assert bar["title"] in bar["status"], bar
    assert dom["where_empty"] == "还没有目标", dom["where_empty"]


def test_侧栏搜索只过滤不发请求(dom: dict) -> None:
    """当前目标留在列表里。没有匹配时有一行提示。Escape 只清搜索。"""
    default = dom["search_default"]
    assert default["ws"] == ["beta", "alpha", "my-tmp-files"], default
    assert "spool-chat-9" not in default["ws"], default
    assert "tmp-notes" not in default["ws"] and "temp-scratch" not in default["ws"], default
    alpha = dom["search_alpha"]
    assert alpha["ws"] == ["beta", "alpha"], alpha
    assert "当前会话" in alpha["sess"] and "Alpha 笔记" in alpha["sess"], alpha
    none = dom["search_none"]
    assert none["ws"] == ["beta"], none
    assert none["sess"] == ["当前会话"], none
    assert "没有匹配的工作区/会话" in none["text"], none
    assert dom["search_cleared"]["ws"] == default["ws"], dom["search_cleared"]
    assert dom["search_alias"]["ws"] == ["beta", "alpha"], dom["search_alias"]
    esc = dom["search_escape"]
    assert esc["value"] == "", esc
    assert esc["ws"] == default["ws"], esc
    assert esc["focus"] == "goal", esc
    assert esc["stopped"] is True, esc
    assert esc["statusOpen"] is True, esc
    assert esc["posts"] == 0, esc
    assert dom["search_escape_global"]["statusOpen"] is False, dom["search_escape_global"]


def test_临时工作区默认隐藏可被搜索找回(dom: dict) -> None:
    """前缀才算临时目录。当前目标即使是临时目录也留在列表里。"""
    assert dom["search_temp"]["ws"] == ["beta", "spool-chat-9"], dom["search_temp"]
    current = dom["search_current_temp"]["ws"]
    assert "spool-chat-9" in current, current
    assert "tmp-notes" not in current and "temp-scratch" not in current, current
    assert "my-tmp-files" in current and "alpha" in current and "beta" in current, current


def test_当前会话可以就地重命名(dom: dict) -> None:
    """铅笔只在当前行。对话框预填并循环焦点。本地拦住空标题和超长标题。"""
    found = dom["session_rename"]
    assert found["pencil"] is True, found
    assert found["other_pencil"] is False, found
    assert found["label"] == "重命名会话", found
    assert found["tab"] == 0, found
    assert found["pencil_focused"] is True, found
    assert found["role"] == "dialog", found
    assert found["modal"] == "true", found
    assert found["open"] is True, found
    assert found["value"] == "A 的第 1 条", found
    assert found["focused"] is True, found
    assert found["selected"] is True, found
    assert found["maxlength"] is None, found
    assert found["click_posts"] == [], found
    assert found["tab_to_close"] is True, found
    assert found["tab_to_save"] is True, found
    assert found["saved_body"] == {"session": "a1", "title": "A 的第 1 条"}, found
    assert found["name"] == "人手标题", found
    assert "人手标题" in found["where"], found
    assert found["source"] == "manual", found
    assert found["stream_same"] is True, found
    assert found["closed"] is True, found
    assert found["success_focus"] == "rename", found
    assert found["empty_error"] == "标题是空的。", found
    assert found["empty_value"] == "   ", found
    assert found["empty_posts"] == 0, found
    assert found["long_error"] == "标题最长 40 个字。", found
    assert found["long_len"] == 41, found
    assert found["long_posts"] == 0, found
    assert found["server_error"] == "没有叫 x 的会话。", found
    assert found["server_value"] == "  保留   输入  ", found
    assert found["server_open"] is True, found
    assert found["server_body"] == {"session": "a1", "title": "保留 输入"}, found
    assert found["esc_hidden"] is True, found
    assert found["esc_focus"] == "a1", found
    assert found["readonly_pencils"] == 0, found
    assert found["archived_pencils"] == 0, found
    assert found["archived_row"] is True, found

