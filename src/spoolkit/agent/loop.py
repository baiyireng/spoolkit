"""Agent 主循环。

设计要点：任务状态每轮注入，对话历史只保留最近若干轮，
因此上下文可以被安全地重置——状态不丢，历史可弃。

预算守卫依据 demand_tokens（裁剪前的需求）而非实际装入量来判断：
装配器最多只能装到有效预算，用实际装入量判断触发线永远不会触发。
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from spoolkit.agent.monitor import ToolSight, inspect
from spoolkit.agent.protocol import ParseFailure, build_turn_schema, parse_turn
from spoolkit.agent.state import TaskState, clear_state, load_state, save_state
from spoolkit import limits as _limits
from spoolkit.agents.supervisor import (
    EXTEND,
    FINISH,
    STOP,
    Evidence,
    Verdict,
    supervise,
)
from spoolkit.config import Config
from spoolkit.context.assembler import Assembler
from spoolkit.context.budget import Budget
from spoolkit.context.sections import Section
from spoolkit.context import templates as T
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.retry import chat_with_escalation
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.llm.types import ChatRequest, Message
from spoolkit.tools.edit import CONFIRM_TOOL, NEXT_STEP_MARKER, REFUSAL_MARKER, STAGE_MARKER
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult
from spoolkit.tools.verify import ENVIRONMENT_MARKER
from spoolkit.errors import ContextOverflowError

SYSTEM_PROMPT = T.SYSTEM

LOOKUP_TOOLS = ("find_symbol", "file_symbols", "find_callers")
EDIT_TOOLS = ("replace_lines", "replace_text", "write_file")

# 「把改动推进下去」的工具：写工具 + 确认入口。
#
# 与 EDIT_TOOLS 分开的理由是它们各自回答不同的问题：EDIT_TOOLS 是**交内容**
# 的那几把（步数预算按它的产出规模放大、验证按它改过的路径分派），而确认入口
# 不交内容、只是一个很小的举证回合。但有一处它必须算进去：漫游太久时循环会把
# 采样空间收窄成「只能写」——收窄之后如果确认入口被语法拿掉，模型就再也
# 确认不了那份暂存的正文了。
COMMIT_TOOLS = EDIT_TOOLS + (CONFIRM_TOOL,)

# 同一个调用连续重复到第几次时加提醒（仍然执行），到第几次时不再执行。
#
# 阈值来自实测，不是拍的：本地 Qwen2.5-Coder-7B 跑回归集时，连续 10 次
# 调用同一个 find_callers（参数一字不差），把 12 步预算全烧光。
# 第二次先给提醒、不阻断——调用有时确实有意义（比如文件刚被改过）；
# 第三次中间没有任何别的动作，结果不可能变，再执行只是在消耗步数。
REPEAT_WARN_AT = int(_limits.knob("repeat_warn_at").default)
REPEAT_BLOCK_AT = int(_limits.knob("repeat_block_at").default)

# 重复到这个次数还停不下来，就升级给督导。
#
# 前面两档是**机械**的（提醒、不执行），它们只挡「参数一模一样」的重复。
# 挡不住的剩下两种情况：一是它换着花样绕（每次都不同，机械计数器永远归零），
# 二是它明知道在重复也停不下来。这两种都得看它到底在干什么才知道怎么办——
# 交给督导判断，而不是继续加规则。
REPEAT_INTERVENE_AT = int(_limits.knob("repeat_intervene_at").default)

# 督导给的续期加起来最多到这里（基础预算的倍数）。
# 这不是任务预算，是安全线：没有它，一个卡住的任务能把 GPU 烧一整夜。
STEP_CEILING_FACTOR = int(_limits.knob("step_ceiling_factor").default)

# 只看「连续相同」会漏掉交替打转：A、B、A、B…每一步都和上一步不同，
# 计数每次都被重置。实测审查者用 git status / git diff 交替复读了 11 步，
# 一次都没被拦住。所以再加一条频率判据：同一个调用在最近几次里出现够多，
# 同样是原地打转，不管中间夹了什么。
RECENT_WINDOW = 6

# 连续多少步没有提出任何改动，就收窄输出通道。
# 重复调用检测只盖得住「参数完全相同」的打转，盖不住「每次都换一个查询」
# 的漫游——实测那条轨迹 12 步里换了 8 种不同的调用，一次都没被拦住。
NO_EDIT_LIMIT = int(_limits.knob("no_edit_limit").default)

# 漫游到这个步数还没产出，就升级给督导。
#
# NO_EDIT_LIMIT 那一步做的是**收窄输出通道**（只能写），它盖得住「它还想查」
# 这一种。盖不住的是「收窄之后它去翻别的文件」——每一步换一个查询，
# 机械计数器永远归零。到这一步该有人看看它到底在干什么，而不是继续加规则。
NO_EDIT_INTERVENE_AT = int(_limits.knob("no_edit_intervene_at").default)

# 空回合连续出现到这个次数就收尾。
#
# 模型想表达「改动提完了、在等你确认」时，只能发出一个空回合；协议里
# 没有这个表达方式，于是它判为无效、回灌、再发一遍。实测连发 11 次，
# 把预算全烧在复读上。它不是要再想一会儿，是卡住了——这时候把已经提出
# 的改动交给用户判断，比继续复读诚实，也便宜。
#
# 阈值取 4 而不是 2：实测阈值太小会误伤——有些题只抖动一两轮就自己走出来了，
# 把它们一起掐掉净亏两道题。真正卡死的那种会一直复读（实测 11 次），
# 放宽到 4 一样能兜住。
EMPTY_TURN_LIMIT = int(_limits.knob("empty_turn_limit").default)


def call_signature(call: ToolCall) -> str:
    """工具调用的指纹：名字 + 规范化后的参数。

    参数按 key 排序后再序列化。不排序的话 {"a":1,"b":2} 与 {"b":2,"a":1}
    会算成两次不同的调用——模型只要换个字段顺序就绕过了检测，
    而它换顺序几乎不花任何代价。
    """
    return call.name + " " + json.dumps(
        call.arguments, sort_keys=True, ensure_ascii=False
    )


# 硬拦（**没有执行**）的标记前缀。
#
# 循环靠它把「被护栏拦下、根本没跑」和「跑了但失败」分开：只有前者的这一步
# 不占步数预算。被拦的动作没有任何产出，也不换来任何新信息——让它白吃一步，
# 等于用预算给模型的原地打转付费（dogfood：两次被拒的 write_file 白吃 2 步）。
BLOCKED_PREFIX = "⛔ 未执行："

# 两类被拦的动作：原样重提（本文件判）与大重写被拒（tools/edit.py 判）。
_BLOCKED_MARKS = (BLOCKED_PREFIX, REFUSAL_MARKER)

# 记住多少个「失败过的调用」。它只是用来打断原样重试，不需要长远记忆；
# 成功改动过文件之后整张表清零（文件变了，同一个参数可能重新有意义）。
_FAILED_MEMORY = 16


def _is_blocked(result: ToolResult) -> bool:
    """这次失败是「被拦下没执行」，而不是「执行了但失败」。"""
    return (not result.ok) and result.content.startswith(_BLOCKED_MARKS)


def _retry_block_message(call: ToolCall, failure: str) -> str:
    """原样重提一个失败过的调用时的话术：**给出能照做的下一步**。

    只说「换一种做法」等于把问题还给模型——dogfood 里它就是这么原样重试
    到预算耗尽的。失败话术里已经带了「照做的下一步」时原样端回来；
    没带时给三条具体可选的替代（换参数、换工具、先读原文）。

    暂存那一种要分开说：它**不是失败**（正文收下了），但把同一份正文再发
    一遍是这次运行里最贵的一种浪费——输出预算正是卡在这里的。
    """
    remedy = [
        line.strip()
        for line in failure.splitlines()
        if line.strip().startswith(NEXT_STEP_MARKER)
    ]
    staged = failure.lstrip().startswith(STAGE_MARKER)
    if staged:
        parts = [
            f"{BLOCKED_PREFIX}这个调用与上一次**完全相同**（工具 {call.name}，"
            "参数一字未改），而上一次的正文**已经收下并暂存**："
            "同样的正文再发一遍不会改变任何事，只会再花一次输出预算，"
            "所以这次没有执行。",
            "要推进下去，只缺「把消失清单逐项交代」这一步——"
            "确认时**只发项 id，不要重发正文**。",
        ]
    else:
        parts = [
            f"{BLOCKED_PREFIX}这个调用与上一次**完全相同**（工具 {call.name}，"
            "参数一字未改），而上一次它失败了：失败的原因不会因为再发一遍而改变，"
            "所以这次没有执行。",
            f"上一次的结论：{' '.join(failure.split())[:300]}",
        ]
    if remedy:
        parts.append(
            "上一次已经给出了能照做的下一步：" if not staged
            else "照做的下一步是这几条（正文已经收下）："
        )
        parts.extend(dict.fromkeys(remedy))
    elif not staged:
        parts.append(
            "换一种做法，三选一：换参数（行号区间、先多带几行上下文）；"
            "换工具（write_file / replace_lines / replace_text）；"
            "先 read_file 确认原文再动手。任务确实已经完成时，直接给出结论。"
        )
    return "\n".join(parts)


def scaled_step_bonus(config: Config, lines: int) -> int:
    """按目标产出规模算出**额外**的步数预算（不含基础预算）。

    公式写在消费点（这里），两个旋钮在 `limits.py` 登记：这样「现在是多少、
    从哪来」走的是 `Config.limit()` 那一条路，而不是在登记表里再养一份实现。

    为什么需要它：dogfood 实测，303 行的两份 HTML 合并成单文档，10 步基线
    用满仍然失败——而这类任务的规模**在第一步就看得见**（文件有多少行是事实），
    所以预算该按规模给，而不是所有任务一律 10 步。
    """
    per = config.limit("step_lines_per_bonus")
    cap = config.limit("step_lines_bonus_cap")
    if per <= 0 or lines <= 0:
        return 0
    return int(min(lines // per, max(cap, 0)))


def _edit_target_lines(config: Config, call: ToolCall) -> int:
    """这次编辑要面对的产出规模（行数）：原文件行数与新内容行数取大。

    它是「步数预算随规模放大」的输入。用两个数的较大者，是因为两种形状都要盖住：
    把一个 300 行的文件改对（原文件大），和交出一份 300 行的新内容（新内容大）。
    """
    if call.name not in EDIT_TOOLS:
        return 0
    path = call.arguments.get("path")
    if not isinstance(path, str) or not path:
        return 0
    target = Path(path)
    if not target.is_absolute():
        target = config.project_root / path
    try:
        old_lines = len(
            target.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        old_lines = 0
    content = call.arguments.get("content")
    new_lines = len(content.splitlines()) if isinstance(content, str) else 0
    return max(old_lines, new_lines)


def _parse_feedback(failure: ParseFailure) -> str:
    """把解析失败翻译成「下一步该做什么」。

    空回合要单独对待：模型不是在写坏格式，而是在表达一件协议里没有的事
    （最常见的是「改动提完了，等用户确认」）。对它说「请只输出规定的 JSON」
    没有任何用——它下一轮会原样再来一次，实测连发十几次。
    """
    if failure.kind == "empty_turn":
        return T.EMPTY_TURN_FEEDBACK
    return (
        f"上一轮输出无法解析：{failure.reason}。"
        "这一轮只输出协议规定的 JSON 对象，不要有其他文字。"
    )


def _edit_fail_hint(streak: int) -> str:
    """编辑工具连续失败时注入的格式提示。

    dogfood 暴露的问题：write_file / replace_text 连续失败后，模型只会原样
    重试——它看到的是错误信息，但不知道正确格式应该长什么样。这里把
    常见错误原因和正确做法直接告诉它，打断死循环。
    """
    return (
        f"编辑工具已连续失败 {streak} 次。不得原样重试，改用另一种做法：\n"
        "- write_file 失败时：确认 path 是相对工作区的路径，不是绝对路径；"
        "content 是文件的完整内容，不是 diff。\n"
        "- replace_text 失败时：old 片段必须在文件里唯一存在。"
        "先用 read_file 看原文，把缩进、空格、引号对上；"
        "片段不唯一时多带几行上下文，使它唯一。\n"
        "- replace_lines 失败时：start_line 与 end_line 是 1 起的行号，"
        "end_line 大于或等于 start_line，且不得超过文件总行数。\n"
        "- 文件不存在时：用 write_file 新建，不要用 replace_ 系列工具。\n"
        "- 连续失败 3 次以上时：先用 read_file 重新确认文件内容，再动手。"
    )


def _tool_sight(call: ToolCall, ok: bool) -> ToolSight:
    """从一次工具调用抽出监测层要的那几个字段。"""
    path = call.arguments.get("path")
    path_s = path if isinstance(path, str) else ""
    query = ""
    for key in ("query", "goal", "pattern"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value:
            query = value
            break
    content_hash = ""
    if call.name in ("write_file", "replace_text", "replace_lines"):
        text = ""
        for key in ("content", "new", "new_text"):
            value = call.arguments.get(key)
            if isinstance(value, str) and value:
                text = value
                break
        if text:
            content_hash = hashlib.sha1(text.encode()).hexdigest()[:12]
    return ToolSight(
        name=call.name,
        path=path_s,
        query=query,
        content_hash=content_hash,
        ok=ok,
    )


def _render_lessons(pushed: list[tuple[int, str]]) -> str:
    """把推送的教训渲染成一段。空的返回空串，不留一个空标题。

    标题里点明来源，是为了让模型知道这是别人踩过的坑、不是当前项目的要求；
    否则它可能把教训当成任务约束照搬。

    标题只从 `T.LESSONS_HEADER` 取：这里原先又硬编码了一遍同样的句子，
    两处字面量会各自走散——改了模板而没改这里，模型看到的抬头与模板
    记录的就不再是同一句，而两处都不会报错。
    """
    if not pushed:
        return ""
    lines = [T.LESSONS_HEADER]
    lines.extend(f"- {text}" for _, text in pushed)
    return "\n".join(lines)


def build_workflow(
    registry: ToolRegistry, read_roots: tuple = (), *, from_channel: bool = False
) -> str:
    """按实际注册的工具生成工作方式说明。

    只写「什么时候用哪个」，不写工具内部怎么实现——模型不需要知道索引是树
    还是图，那是纯浪费。

    关键在于每一句都从注册表推导。提到一个没注册的工具，就是给模型一条它
    做不到的指令，比不写更糟：它会浪费步数去尝试。审查者没有写权限，
    所以它看到的说明里就不该出现写工具。
    """
    lines = ["工作方式："]

    lookup = [name for name in LOOKUP_TOOLS if registry.get(name) is not None]
    edits = [name for name in EDIT_TOOLS if registry.get(name) is not None]
    if registry.get("survey") is not None:
        # 放在查询规则**前面**：它管的是「动手前那一趟」，比单个查询更靠前。
        lines.append(T.WORKFLOW_SURVEY)
    if lookup:
        lines.append(T.WORKFLOW_FIND.format(names=" / ".join(lookup)))
    if registry.get("find_callers") is not None:
        lines.append(T.WORKFLOW_IMPACT)
    if lookup and edits:
        # 必须紧跟在查询规则后面。实测模型会把「怎么查」那一段当成全部
        # 工作方式，从头查到尾、一次改动都不提；这条是它的对面。
        # 只读角色（没有写工具）不得看到这句——那是它做不到的事。
        lines.append(T.WORKFLOW_ACT)

    if edits:
        if "write_file" in edits and "replace_text" in edits:
            lines.append(T.WORKFLOW_EDIT_FULL)
        elif "write_file" in edits:
            # 小项目只给整份重写：这时候再说「精确替换」就是在提一个
            # 不存在的工具，模型会浪费步数去找它。
            lines.append(T.WORKFLOW_EDIT_WHOLE)
        else:
            lines.append(T.WORKFLOW_EDIT_PRECISE)
        if "replace_lines" in edits:
            lines.append(T.WORKFLOW_EDIT_LINES)
        lines.append(T.WORKFLOW_WRITE_SAFE)
        if "write_file" in edits:
            # 紧跟改动方式：它管的正是「那几条路都被拦下时怎么办」。
            lines.append(T.WORKFLOW_REWRITE_DECLARE)

    if registry.get("recall") is not None:
        lines.append(T.WORKFLOW_RECALL)

    if registry.get("search_history") is not None:
        # 紧挨着 recall：两条都是「过去的事按需取」，而这条的关键是
        # 「默认不检索」——不说清，模型会把它当成每轮必做的动作。
        lines.append(T.WORKFLOW_HISTORY)

    if registry.get("run_command") is not None:
        lines.append(T.WORKFLOW_RUN)
        if registry.get("verify_file") is not None:
            # 紧跟「怎么验证」：上一句说系统会自动验，这一段说按什么类型验、
            # 用哪个工具——两句话是同一件事的两半。没注册这个工具时不写，
            # 免得给模型一条它做不到的指令。
            lines.append(T.WORKFLOW_VERIFY)
        # 紧跟「怎么验证」：它是"验证命令本身写错"那一类失败的补丁
        # （实测模型自己发明导入路径，一路验证失败还以为是代码错了）。
        lines.append(T.WORKFLOW_CONVENTIONS)
        lines.append(T.WORKFLOW_TESTS_ARE_SPEC)
        lines.append(T.WORKFLOW_TEST_SCOPE)

    if registry.get("request_diagnosis") is not None:
        # 紧跟在「怎么验证」后面：它处理的正是验证本身出问题的那种情况。
        lines.append(T.WORKFLOW_DIAGNOSIS)

    if read_roots:
        lines.append(
            T.WORKFLOW_READ_ROOTS.format(
                roots="、".join(str(item) for item in read_roots)
            )
        )

    if registry.get("research") is not None:
        # 有独立研究会话时，主循环不该自己把整页灌进来。不可信边界仍要说。
        lines.append(T.WORKFLOW_RESEARCH)
    elif registry.get("web_fetch") is not None:
        # 紧跟"可读目录"：两者都是"工作区之外的输入"，而网页那条多一层
        # 不可信的边界，必须紧挨着说。
        lines.append(T.WORKFLOW_WEB)

    if registry.get("dir_stats") is not None:
        lines.append(T.WORKFLOW_STATS)

    if registry.get("calc") is not None:
        lines.append(T.WORKFLOW_CALC)
        lines.append(T.WORKFLOW_PERMISSION)
        lines.append(T.WORKFLOW_DEPENDENCY)

    if registry.get("dispatch") is not None:
        # 放在最后几条之前：它讲的是「怎么把活干完」的选择，
        # 而不是某类工具的用法，跟前面的查询/改动规则并列。
        lines.append(T.WORKFLOW_DISPATCH)

    if from_channel:
        lines.append(T.WORKFLOW_CHANNEL_SCOPE)

    lines.append(T.WORKFLOW_NO_GUESS)
    return "\n".join(lines)

MAX_RECENT_TURNS = 6

# 工具输出短于这个长度就不去重：短内容本来就不值钱，比对反而增加噪音。
REPEAT_MIN_CHARS = 200


def _is_repeat_output(content: str, prefetched: str, seen: set[str]) -> bool:
    """这段工具输出是不是「上面已经有了」。

    为什么要它：预取（锚定 + 关键词）已经把这一步要改的文件正文放进提示词了，
    而模型经常还是先 `read_file` 看一遍——同一段正文于是在提示词里出现两次，
    并且**每一次后续调用**都带着这两份。实测 50 题那轮，工具输出来自读文件，
    读到的内容原样留在最近几轮里。

    判定是「正文完整出现过」：要么就是这段提示词里已有（预取），要么本轮
    已经返回过一模一样的一份。两种情况都不丢信息——相同正文仍在提示词里，
    只是不再重复第二遍。
    """
    body = content.strip()
    if len(body) < REPEAT_MIN_CHARS:
        return False
    if body in seen:
        return True
    seen.add(body)
    return body in prefetched

# 只有这几类工具的结果算「进度」。读文件和搜索不算：它们是手段，
# 不是产出，记进去只会把状态撑满噪声。
PROGRESS_TOOLS = ("write_file", "replace_lines", "run_command")
# 「已完成」在状态块里列几条。它每轮都注入，所以是每个请求的税——
# 完整记录留在状态与 .agent/progress.md 里（见 TaskState.DONE_INLINE）。
MAX_DONE_NOTES = TaskState.DONE_INLINE


@dataclass
class LoopResult:
    """一次运行的结果。"""

    finished: bool
    final: str
    state: TaskState
    steps: int
    resets: int
    trace: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    # 时间花在哪两笔上。**必须分开**：「等模型生成」和「跑工具」的优化方向
    # 完全不同——前者靠拆小任务/换模型，后者靠少起进程、少拷副本。
    # 只有总耗时的话，两种编排差一倍也看不出该动哪边。
    model_seconds: float = 0.0
    tool_seconds: float = 0.0
    # 账上没人认领的那部分要拆开——原先只有「模型 + 工具」两笔，
    # 剩下的全落进"其它"，而"其它"这个东西是调不动的。这里把其余三笔
    # 也计出来：装配上下文、自动验证、落盘（检查点/进度）。
    assemble_seconds: float = 0.0
    verify_seconds: float = 0.0
    state_seconds: float = 0.0
    supervisor_seconds: float = 0.0
    # 装配这一次循环本身（建索引、装注册表、算预取）。它发生在 run() 之外，
    # 所以由调用方填进来——按步执行那条路上，它每步都要付一次。
    wiring_seconds: float = 0.0
    lessons_pushed: tuple[int, ...] = ()
    # 这次运行里确认过「失败是环境造成的」。任务没做成时，它是决定
    # 要不要替 Agent 登记诊断请求的依据之一。
    environment_blocked: bool = False

    def usage(self) -> str:
        """一行用量摘要，用于比较不同配置的实际成本。"""
        return (
            f"步数 {self.steps}，模型调用 {self.model_calls}，"
            f"输入 {self.prompt_tokens} token，输出 {self.completion_tokens} token，"
            f"模型 {self.model_seconds:.1f}s / 工具 {self.tool_seconds:.1f}s"
            f" / 装配 {self.assemble_seconds:.1f}s / 验证 {self.verify_seconds:.1f}s"
            f" / 落盘 {self.state_seconds:.1f}s"
            + (
                f" / 督导 {self.supervisor_seconds:.1f}s"
                if self.supervisor_seconds
                else ""
            )
        )


class AgentLoop:
    """单步编排的主循环。"""

    def __init__(
        self,
        gateway: ModelGateway,
        tokenizer: TokenCounter,
        registry: ToolRegistry,
        config: Config,
        prefetch: Callable[[str], str] | None = None,
        memory: Any | None = None,
        distiller: Callable[[TaskState, str], list[tuple[str, str]]] | None = None,
        lessons: Callable[[str], list[tuple[int, str]]] | None = None,
        verify: Callable[[], ToolResult] | None = None,
        incoming: Callable[[], str] | None = None,
        read_roots: tuple = (),
        sources: Any | None = None,
        persona: str = "",
        on_event: Callable[[str, dict], None] | None = None,
        from_channel: bool = False,
    ) -> None:
        self.gateway = gateway
        self.tokenizer = tokenizer
        self.registry = registry
        self.config = config
        self.prefetch = prefetch
        self.memory = memory
        self.distiller = distiller
        self.lessons = lessons
        self.verify = verify
        self.incoming = incoming
        self.read_roots = tuple(read_roots)
        # 工具输出台账：数字核对要拿它当「出处」。由循环追加，
        # 模型改不了它——模型自己写进去的出处不算出处。
        self.sources = sources
        self.environment_blocked = False
        self.persona = persona
        self.on_event = on_event
        self.from_channel = from_channel
        # 失败过的调用（指纹 → 次数 / 上次的失败话术）。用来硬拦原样重试，
        # 并把它失败时看过的「照做的下一步」端回去。每次 run() 开始时清空。
        self._failed_calls: dict[str, int] = {}
        self._failed_output: dict[str, str] = {}
        # 预算里的每个比例都是能力标定值：走本次运行的覆盖，而不是各写一份常量。
        self._budget = Budget(
            window=config.context_window, overrides=config.overrides
        )
        self._schema = build_turn_schema(registry)
        # 强制收敛用的收窄 schema：只留写工具（以及确认入口——见 COMMIT_TOOLS
        # 的注释：收窄把确认入口拿掉的话，暂存的正文就没人能确认了）。
        # 没有写工具（只读角色）就没有这回事，None 表示这条路不适用。
        edits = [name for name in COMMIT_TOOLS if registry.get(name) is not None]
        self._commit_schema = build_turn_schema(registry, only=edits) if edits else None

    def _assemble(
        self,
        state: TaskState,
        history: list[Message],
        feedback: str | None,
        prefetched: str = "",
        hot: str = "",
        lesson_text: str = "",
    ):
        """按当前状态与历史装配本轮上下文。

        预取内容与解析反馈共用 retrieval 预算：它们语义相同，都是外部检索来的内容。
        """
        assembler = Assembler(tokenizer=self.tokenizer, budget=self._budget)
        system_text = SYSTEM_PROMPT.format(
            tools=self.registry.describe(),
            workflow=build_workflow(
                self.registry, self.read_roots, from_channel=self.from_channel
            ),
        )
        if self.persona:
            system_text = f"{self.persona}\n\n{system_text}"
        sections = [
            Section(name="system", text=system_text, priority=10, mandatory=True),
        ]
        if hot:
            sections.append(Section(name="hot_memory", text=hot, priority=20))
        if lesson_text:
            sections.append(Section(name="lessons", text=lesson_text, priority=25))
        sections.append(
            Section(
                name="task_state",
                # 「已完成」列几条是能力标定值：强模型可以多列几条，
                # 但那是每个请求的税，所以由本次运行的覆盖决定。
                text=state.render(
                    int(self.config.limit("done_inline")),
                    progress_hint=self.config.progress_hint,
                ),
                priority=30,
            )
        )
        if prefetched:
            sections.append(Section(name="retrieval", text=prefetched, priority=35))
        if feedback:
            sections.append(Section(name="retrieval", text=feedback, priority=40))
        return assembler.assemble(sections, recent_turns=history[-MAX_RECENT_TURNS:])

    def run(
        self,
        goal: str,
        task_id: str = "task",
        resume: bool = False,
        prompt: str | None = None,
    ) -> LoopResult:
        """运行任务直到给出最终答复或达到步数上限。

        `goal` 是**短目标**（进状态块、进检查点、给督导看）；
        `prompt` 是这一轮真正发给模型的第一段话，不给就等于 goal。

        为什么要分开：按步执行那条路上，原先给的是整段步骤提示词，于是
        状态块里又原样重复了一遍（"目标: <整段提示词>"）——每一次调用都付
        两份。分开之后状态块里是那句短目标，读起来也更像"我做到哪儿了"。
        """
        checkpoint = self.config.task_path(task_id)

        # 恢复未完成的检查点。之前这里只写不读，等于「崩溃可恢复」这句
        # 承诺从未兑现——跑到一半中断，下次只能从零开始。
        state = load_state(checkpoint) if resume else None
        resumed = state is not None
        if state is None:
            state = TaskState(task_id=task_id, goal=goal)
        goal = state.goal

        # 续跑给的是**新增**预算，不是沿用已经耗尽的那份。
        # 否则一个撞过上限的任务永远续不动——检查点里的步数已经等于上限了。
        limit = state.step + self.config.max_steps if resumed else self.config.max_steps
        # 总步数上限：督导的续期从这里扣。基础预算只是**起点**，
        # 一次跑多久由督导按「有没有进展」决定，这个数是它的天花板。
        ceiling = self.config.step_ceiling or int(
            self.config.max_steps * self.config.limit("step_ceiling_factor")
        )
        ceiling = max(ceiling, limit)

        history: list[Message] = [
            Message(
                role="user",
                content=(
                    f"继续之前未完成的任务：已经进行 {state.step} 步。"
                    if resumed
                    else (prompt or goal)
                ),
            )
        ]
        feedback: str | None = None
        # 本次任务里已经出现过的工具输出正文。同一步里第二遍读到同一份内容时，
        # 只在历史里留一行说明——省下的正是「每轮都要重发一遍」的那部分。
        seen_outputs: set[str] = set()
        # 督导让主循环改道时说的话。单独放一个变量，是因为 feedback 会被
        # 强制收敛那几处覆盖掉——而改道的话比通用提醒具体，不能被覆盖。
        redirect: str | None = None
        resets = 0
        trace: list[str] = []
        # 上一轮诊断回来的报告：一开局就摆到面前，模型不用记得去查。
        if self.incoming is not None:
            report = self.incoming()
            if report:
                feedback = report
                trace.append("注入诊断报告")
        prompt_tokens = 0
        completion_tokens = 0
        model_calls = 0
        model_seconds = 0.0
        tool_seconds = 0.0
        assemble_seconds = 0.0
        verify_seconds = 0.0
        state_seconds = 0.0
        supervisor_seconds = 0.0
        prefetched = self.prefetch(goal) if self.prefetch is not None else ""
        hot = self.memory.hot_text() if self.memory is not None else ""
        # 「原地打转」检测：连续相同的调用计数。跨步骤累计，
        # 中间只要出现一个不同的调用就清零。
        last_signature = ""
        repeats = 0
        recent: list[str] = []
        # 监测窗口：每次工具调用记一条，跨步累计；任务结束随函数丢掉。
        sights: list[ToolSight] = []
        cycle_streak = 0
        # 编辑工具连续失败计数。跨步骤累计，成功一次就清零。
        # 达到 2 时往工具输出里追加格式提示——模型看到错误后只会原样重试，
        # 不告诉它正确格式它就死循环（dogfood：贪吃蛇第 2 步连续 4 次 write_file 失败）。
        edit_fail_streak = 0
        no_edit_steps = 0
        empty_turns = 0
        overflow_retried = False
        # 督导连续问几次就该退避：同一个打转状态每步问一遍，问出来的话是一样的，
        # 只是把成本翻倍。所以每次介入之后把门槛翻倍（4 → 8 → 16），
        # 而不是设一个「隔几步问一次」的定时器。
        intervene_at = int(self.config.limit("repeat_intervene_at"))
        roam_intervene_at = int(self.config.limit("no_edit_intervene_at"))
        cycle_at = int(self.config.limit("name_cycle_at"))
        oscillation_at = int(self.config.limit("oscillation_at"))
        same_query_at = int(self.config.limit("same_query_at"))
        edits_made = 0
        tool_calls_made = 0
        stopped_by = ""
        # 「步数预算随规模放大」已经补上去几步；只增不减，避免同一个目标
        # 反复把上限抬来抬去（上限抖动会让督导看到的 limit 说不清）。
        size_bonus = 0
        # 被硬拦的动作退还了几步（没有产出，不该白吃预算）。有上限——
        # 没有上限的话，模型可以靠不断被拦把预算变成无限。
        refunds_used = 0
        self._clear_failures()

        # 主动推送：进入任务时就把它相关的历史教训放到模型面前，
        # 而不是等它自己去检索——它不会去检索，因为它不知道自己缺什么。
        pushed: list[tuple[int, str]] = (
            list(self.lessons(goal)) if self.lessons is not None else []
        )
        lesson_text = _render_lessons(pushed)
        if pushed:
            # 让推送可见：教训推了没推、推了哪几条，是调这块时唯一能看到的信号。
            trace.append(f"推送 {len(pushed)} 条历史教训：" + "；".join(
                text[:30] for _, text in pushed
            ))

        while True:
            if state.step >= limit and not self.config.supervise:
                # 关掉督导就退回老行为：撞上上限即停。这条路仍然要留着——
                # 它是一个可用的对照，也是督导自己出问题时的最终退路。
                break
            stuck_on_repeat = self.config.supervise and repeats >= intervene_at
            # 换着花样绕：每步都换了查询，重复计数永远归零，只有「一直没产出」
            # 这个信号还在涨。
            stuck_on_roam = (
                self.config.supervise and no_edit_steps >= roam_intervene_at
            )
            report = inspect(
                sights,
                window=int(self.config.limit("name_cycle_window")),
            )
            # A→A 是重复或漫游已经在管的事；短环要的是换参的 A→B→A→B。
            distinct = {name for name in report.cycle.split("→") if name}
            if len(distinct) > 1:
                cycle_streak += 1
            else:
                cycle_streak = 0
            stuck_on_cycle = self.config.supervise and cycle_streak >= cycle_at
            stuck_on_oscillation = (
                self.config.supervise and report.oscillations >= oscillation_at
            )
            stuck_on_query = (
                self.config.supervise and report.same_query >= same_query_at
            )
            if (
                state.step >= limit
                or stuck_on_repeat
                or stuck_on_roam
                or stuck_on_cycle
                or stuck_on_oscillation
                or stuck_on_query
            ):
                # 两个触发点，同一件事：机械层已经说了有事，而模型自己没纠正。
                # 这是**要判断**的点，不是失败点——原先的处理是停下来提示用户
                # `--resume`，那等于把判断成本推给用户，还把一件事拆成两次对话。
                exhausted = state.step >= limit
                if exhausted:
                    asking = "这一轮的步数预算用完了，任务还没结束"
                elif stuck_on_cycle:
                    asking = (
                        f"短环：工具路径在 {report.cycle} 打转，"
                        "参数每次都不同，精确指纹抓不到"
                    )
                elif stuck_on_oscillation:
                    asking = (
                        f"同一文件内容来回覆盖 {report.oscillations} 次，"
                        "改动可能相互抵消"
                    )
                elif stuck_on_query:
                    asking = f"同一检索词已经查了 {report.same_query} 次"
                elif stuck_on_roam:
                    asking = f"它已经连续 {no_edit_steps} 步只看不写，没有任何产出"
                else:
                    asking = (
                        f"它已经连续 {repeats} 次调用同一个工具，参数也相同，仍在重复"
                    )
                # 把「为什么把它叫起来」记下来：调这块时唯一能看的东西就是它。
                trace.append(f"step{state.step}: 叫督导——{asking}")
                _supervisor_started = time.time()
                verdict = self._ask_supervisor(
                    state,
                    trace,
                    ceiling=ceiling,
                    limit=limit,
                    edits=edits_made,
                    calls=tool_calls_made,
                    resets=resets,
                    repeats=repeats,
                    no_edit=no_edit_steps,
                    tokens=prompt_tokens + completion_tokens,
                    asking=asking,
                    cycle=report.cycle,
                    oscillations=report.oscillations,
                    same_query=report.same_query,
                )
                supervisor_seconds += time.time() - _supervisor_started
                if verdict is not None:
                    prompt_tokens += verdict.prompt_tokens
                    completion_tokens += verdict.completion_tokens
                    model_calls += 1
                if verdict is None:
                    # 拿不到结论时按**不敢续期**处理：含糊的续期比不续期危险——
                    # 不续期只是停下来，错续期会把预算继续投进一个卡住的任务。
                    # 打转那条不打断任务，只退避：它还没到花光预算的地步。
                    intervene_at = max(repeats * 2, intervene_at * 2)
                    roam_intervene_at = max(
                        no_edit_steps * 2, roam_intervene_at * 2
                    )
                    cycle_at = max(cycle_streak * 2, cycle_at * 2)
                    cycle_streak = 0
                    oscillation_at = max(
                        report.oscillations * 2, oscillation_at * 2
                    )
                    same_query_at = max(
                        report.same_query * 2, same_query_at * 2
                    )
                    if exhausted:
                        stopped_by = "督导没能给出结论（调用失败或结论不合格式）"
                        trace.append(
                            f"step{state.step}: 督导没给结论，按预算用尽收尾"
                        )
                        break
                elif verdict.action == STOP:
                    # 督导说停就直接停——继续烧到上限并不比它诚实。
                    stopped_by = verdict.reason or "督导判断这个任务做不下去"
                    trace.append(f"step{state.step}: 督导建议收手——{stopped_by}")
                    break
                elif verdict.action == FINISH:
                    # 「它已经做完了，只是自己没宣告」。这条以前不存在，
                    # 于是出现自相矛盾的收尾（「任务没做完，收手的原因：
                    # 任务已完成」），而后果是这次工作被记成失败——
                    # 派发路径里就表现为「审查没得出结论」。
                    done_text = verdict.message or verdict.reason
                    trace.append(
                        f"step{state.step}: 督导判断已完成——{verdict.reason}"
                    )
                    self._archive(state, "success", trace, done_text)
                    clear_state(checkpoint)
                    return LoopResult(
                        finished=True,
                        final=done_text,
                        state=state,
                        steps=state.step,
                        resets=resets,
                        trace=trace,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        model_calls=model_calls,
                        model_seconds=model_seconds,
                        tool_seconds=tool_seconds,
                        assemble_seconds=assemble_seconds,
                        verify_seconds=verify_seconds,
                        state_seconds=state_seconds,
                        supervisor_seconds=supervisor_seconds,
                        lessons_pushed=tuple(item[0] for item in pushed),
                        environment_blocked=self.environment_blocked,
                    )
                else:
                    if verdict.grants:
                        limit = state.step + verdict.grants
                    if verdict.message:
                        redirect = verdict.message
                    what = (
                        f"续 {verdict.grants} 步"
                        if verdict.action == EXTEND
                        else f"改道（续 {verdict.grants} 步）"
                    )
                    trace.append(
                        f"step{state.step}: 督导{what}（预算 {limit}）——{verdict.reason}"
                    )
                    if verdict.message:
                        trace.append(f"step{state.step}: 发给它的话：{verdict.message}")
                    if not exhausted:
                        # 同一个打转状态每步问一遍，问出来的话是一样的，
                        # 只是把成本翻倍。门槛翻倍，别设定时器。
                        intervene_at = max(repeats * 2, intervene_at * 2)
                        roam_intervene_at = max(
                            no_edit_steps * 2, roam_intervene_at * 2
                        )
                        cycle_at = max(cycle_streak * 2, cycle_at * 2)
                        cycle_streak = 0
                        oscillation_at = max(
                            report.oscillations * 2, oscillation_at * 2
                        )
                        same_query_at = max(
                            report.same_query * 2, same_query_at * 2
                        )
            # 强制收敛：连续若干步只查看不修改时，把「继续查」这个选项从
            # 语法里拿掉。实测这个模型对文字提醒完全免疫（重复提醒、拦截
            # 说明都照发不误），能推得动它的只有「这一轮物理上只能选什么」。
            forcing = (
                self._commit_schema is not None
                and no_edit_steps >= self.config.limit("no_edit_limit")
            )
            # 只读角色没有写工具，收窄无从谈起——对它来说唯一的「推进」
            # 就是给结论。没有这股压力，它会一路翻文件翻到步数上限。
            concluding = (
                self._commit_schema is None
                and no_edit_steps >= self.config.limit("no_edit_limit")
            )
            if forcing:
                feedback = T.FORCE_COMMIT
            elif concluding:
                feedback = T.FORCE_CONCLUDE
            # 督导的话排在最后：它比通用提醒具体，不该被顶掉。只发一次——
            # 一直挂着会让每一轮的装配都多一段重复内容。
            if redirect is not None:
                feedback = redirect
                redirect = None
            _assemble_started = time.time()
            assembled = self._assemble(
                state, history, feedback, prefetched, hot, lesson_text
            )
            assemble_seconds += time.time() - _assemble_started
            self._emit("step", {"n": state.step})

            # 预算守卫：软触发整理，硬触发重置。依据需求体积而非装入量。
            if assembled.demand_tokens >= self._budget.hard_limit():
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(f"step{state.step}: 上下文重置（第 {resets} 次）")
                _assemble_started = time.time()
                assembled = self._assemble(
                    state, history, feedback, prefetched, hot, lesson_text
                )
                assemble_seconds += time.time() - _assemble_started
            elif assembled.demand_tokens >= self._budget.soft_limit():
                history = history[-(MAX_RECENT_TURNS // 2):]
                trace.append(f"step{state.step}: 上下文整理")

            try:
                _call_started = time.time()
                response = chat_with_escalation(
                    self.gateway,
                    ChatRequest(
                        messages=assembled.messages,
                        max_tokens=self._budget.output_reserve(),
                        response_schema=(
                            self._commit_schema if forcing else self._schema
                        ),
                    ),
                )
                model_seconds += time.time() - _call_started
            except ContextOverflowError:
                model_seconds += time.time() - _call_started
                # 预算用的是**估算**分词器，服务端用的是真实分词；两者偏差大时
                # （中文/代码混排尤其明显）本地判定会放行、服务端拒绝。
                # 这不能让它把整个运行打断——丢掉历史重发一次，只重试一次。
                if overflow_retried:
                    raise
                overflow_retried = True
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(
                    f"step{state.step}: 服务端说提示词超长，丢掉历史重发（第 {resets} 次）"
                )
                continue
            model_calls += 1
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            turn = parse_turn(response.text)

            if isinstance(turn, ParseFailure):
                if turn.kind == "empty_turn":
                    empty_turns += 1
                    if empty_turns >= self.config.limit("empty_turn_limit"):
                        # 它不是在想，是卡住了。继续复读只会把预算烧光，
                        # 而已提出的改动是真实产出——交给用户判断。
                        trace.append(
                            f"step{state.step}: 连续 {empty_turns} 轮空回合，"
                            "模型卡死，提前收尾"
                        )
                        self._archive(state, "fail", trace, "")
                        return LoopResult(
                            finished=False,
                            final=T.EMPTY_TURN_FINAL,
                            state=state,
                            steps=state.step,
                            resets=resets,
                            trace=trace,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            model_calls=model_calls,
                            model_seconds=model_seconds,
                            tool_seconds=tool_seconds,
                            assemble_seconds=assemble_seconds,
                            verify_seconds=verify_seconds,
                            state_seconds=state_seconds,
                            supervisor_seconds=supervisor_seconds,
                            lessons_pushed=tuple(item[0] for item in pushed),
                        )
                else:
                    empty_turns = 0
                if turn.kind == "truncated":
                    # 截断说明「这个任务的输出形态就是偏大」。与其每一轮都撞一次
                    # 再翻倍重试，不如把这个事实记在这次任务里（封顶见 Budget）。
                    before = self._budget.output_reserve()
                    after = self._budget.boost_output()
                    if after > before:
                        trace.append(
                            f"step{state.step}: 输出被截断，本次任务的输出预算 "
                            f"{before} → {after} token"
                        )
                    # 光抬高预算不够：它得知道「这一轮要少说点」，
                    # 否则重来的还是同样大的一份。
                    feedback = T.OUTPUT_TRUNCATED.format(limit=after)
                else:
                    feedback = _parse_feedback(turn)
                history.append(Message(role="assistant", content=response.text))
                history.append(Message(role="user", content=feedback))
                state.step_forward()
                # 解析失败也要落盘：步数确实推进了，不写的话这类失败
                # 会连一个可续跑的检查点都不留下。
                _state_started = time.time()
                save_state(state, checkpoint)
                state_seconds += time.time() - _state_started
                snippet = response.text.strip().replace("\n", " ")[:160]
                trace.append(f"step{state.step}: 解析失败 - {turn.reason} | 原始: {snippet}")
                continue

            if turn.state_delta is not None:
                state.apply(turn.state_delta)
            history.append(Message(role="assistant", content=response.text))

            blocked_all = False
            edited = False
            if turn.tool_calls:
                _tools_started = time.time()
                outputs = []
                repeated = 0
                # 这一步的调用是不是**全部**被硬拦（没有执行）。是的话这一步
                # 不占步数预算——被拦的动作没有产出，不该白吃预算。
                blocked_all = True
                # 这一轮改过哪些文件。自动验证靠它判断「该在哪儿验」——
                # 一个工作区里有很多件事时，范围跟着改动走才对得上因果。
                changed: list[str] = []
                for call in turn.tool_calls:
                    signature = call_signature(call)
                    repeats = repeats + 1 if signature == last_signature else 1
                    last_signature = signature
                    recent.append(signature)
                    del recent[:-RECENT_WINDOW]
                    # 两条判据各算各的：连续相同按连续次数算，交替打转按出现频率算。
                    # 分开是因为文案要说实话——「和上一步完全相同」在交替打转时
                    # 是假的，上一步明明是别的调用。
                    seen = recent.count(signature)
                    level = max(repeats, seen)
                    # 步数预算随产出规模放大。放在执行**之前**：被拦下的那次
                    # 调用同样告诉我们「这个文件有多大」，而预算该在撞上墙之前
                    # 就长上去——dogfood 那次 10 步用满时已经晚了。
                    target_lines = _edit_target_lines(self.config, call)
                    # 输出预算也随规模放大，而且是**在生成之前**抬。
                    #
                    # 原来只在「被截断之后」才抬。dogfood 实测：一份 120 行的整份
                    # 产出在 0.15 档（本机 8K 窗口 = 1225 token）就是不够——那一次
                    # 被截断，重试才拿到 2455（两次 r3 复跑都是这个形状）。截断要付
                    # 两次钱：这一轮的生成白烧；而且模型收到「把活拆小」的反馈后可能
                    # 把 rewrite_reason 一起丢掉（r3 第一次跑就是这么卡在护栏外面的）。
                    # 「这次要交多长」在第一步就看得见（文件行数），与步数预算同一个判据。
                    boost_at = int(self.config.limit("output_boost_min_lines"))
                    if boost_at > 0 and target_lines >= boost_at:
                        before = self._budget.output_reserve()
                        after = self._budget.boost_output()
                        if after > before:
                            trace.append(
                                f"step{state.step}: 目标产出要 {target_lines} 行，"
                                f"输出预算按规模放大 {before} → {after} token"
                            )
                    bonus = scaled_step_bonus(self.config, target_lines)
                    if bonus > size_bonus:
                        size_bonus = bonus
                        grown = max(limit, self.config.max_steps + bonus)
                        if grown > limit:
                            limit = grown
                            ceiling = max(ceiling, limit)
                            trace.append(
                                f"step{state.step}: 目标产出规模要 {target_lines} 行，"
                                f"步数预算按规模放大到 {limit}"
                            )
                    result = self._invoke_guarded(call, repeats, seen)
                    blocked = _is_blocked(result)
                    if not blocked:
                        blocked_all = False
                    sights.append(_tool_sight(call, result.ok))
                    tool_calls_made += 1
                    if result.usage is not None:
                        # 工具自己花的模型开销（派发那一趟）也要记账：
                        # 不记的话用量表报的是「主循环自己花了多少」，
                        # 而人会把它读成「这次任务花了多少」。
                        prompt_tokens += result.usage.prompt_tokens
                        completion_tokens += result.usage.completion_tokens
                        model_calls += result.usage.calls
                    if self.sources is not None:
                        self.sources.add(result.content, result.facts)
                    self._note_progress(state, call, result)
                    self._write_progress(state)
                    if (
                        result.ok
                        and call.name in COMMIT_TOOLS
                        and not result.content.startswith(STAGE_MARKER)
                    ):
                        # 暂存**不算产出**：正文收下了，但文件还没落盘。把它算成
                        # 产出的话，循环会去「验证这次改动」——而没有待确认的改动，
                        # 验证只能报「没有需要验证的改动」，那是一句假通过。
                        edited = True
                        edits_made += 1
                        edit_fail_streak = 0
                        # 文件真的变了：失败表清零——旧参数可能重新有意义。
                        self._clear_failures()
                        path = call.arguments.get("path")
                        if isinstance(path, str) and path:
                            changed.append(path)
                    elif not result.ok and call.name in COMMIT_TOOLS and not blocked:
                        # 被硬拦的不计进「连续失败」：它没执行，也不是格式错误，
                        # 而且拦它的话术本身已经给了照做的下一步。
                        edit_fail_streak += 1
                    if level >= self.config.limit("repeat_warn_at"):
                        trace.append(
                            f"step{state.step}: 重复调用第 {level} 次：{call.name}"
                        )
                    repeats = level
                    payload = {
                        "name": call.name,
                        "ok": result.ok,
                        "detail": " ".join(result.content.split())[:200],
                    }
                    if result.usage is not None:
                        # 派发 / research 自己花的账。总用量把它们加进去了，
                        # 量「主循环省了多少」时要从这里减回来。
                        payload["prompt_tokens"] = result.usage.prompt_tokens
                        payload["completion_tokens"] = result.usage.completion_tokens
                        payload["calls"] = result.usage.calls
                    self._emit("tool", payload)
                    status = "成功" if result.ok else "失败"
                    if _is_repeat_output(result.content, prefetched, seen_outputs):
                        # 相同正文仍在提示词里（预取或上一轮），只是不再重复第二遍。
                        outputs.append(
                            f"[{call.name}] {status}: （这段内容上面已经给过，未重复）"
                        )
                        repeated += 1
                    else:
                        outputs.append(f"[{call.name}] {status}: {result.content}")
                    # 失败时把输出压成一行摘要。取第一行不行：那里是命令本身，
                    # 而不是错误原因——调试时会被误导。
                    # 失败信息给足长度：160 字刚好够看到命令本身，
                    # 而真正的原因在后面——调试时会被自己的截断挡住。
                    flat = " ".join(result.content.split())
                    detail = "" if result.ok else f": {flat[:400]}"
                    trace.append(f"step{state.step}: 工具 {call.name} -> {status}{detail}")
                tool_seconds += time.time() - _tools_started
                if repeated:
                    trace.append(
                        f"step{state.step}: {repeated} 条工具输出的内容与上面重复，"
                        "没有重复计入上下文"
                    )
                # 编辑工具连续失败 ≥2 次：追加格式提示，打断「原样重试」死循环。
                # 模型看到错误后只会用同样方式再试——不告诉它正确格式，它就转不出来。
                if edit_fail_streak >= 2:
                    hint = _edit_fail_hint(edit_fail_streak)
                    outputs.append(hint)
                    trace.append(
                        f"step{state.step}: 编辑工具连续失败 {edit_fail_streak} 次，"
                        "已注入格式提示"
                    )
                history.append(Message(role="tool", content="\n".join(outputs)))

                # 改完就替它验一次。模型不会主动去跑测试——实测 8 条失败里
                # 一条 run_command 都没有——所以这件事由循环来做，把结果
                # 当场顶回去，它才有机会发现自己改错了。
                if edited and self.verify is not None:
                    _verify_started = time.time()
                    started = time.time()
                    report, passed = self._run_verification(changed)
                    # 验证单独计一笔：它是**起子进程跑测试**，优化方向
                    # （换更快的测试、只跑相关文件）与工具那条完全不同。
                    verify_seconds += time.time() - _verify_started
                    trace.append(
                        f"step{state.step}: 自动验证 -> {'通过' if passed else '失败'}"
                        f"（{len(changed)} 处改动，{time.time() - started:.1f}s）"
                    )
                    self._emit(
                        "tool",
                        {
                            "name": "自动验证",
                            "ok": passed,
                            "detail": " ".join(report.split())[:200],
                        },
                    )
                    # 必须紧跟在那次改动后面，不能塞进 system 区段。
                    # 区段排在整段历史之前，模型会先读到「测试失败」、
                    # 再读到自己刚才做的事——因果顺序反了，它接不上。
                    history.append(Message(role="user", content=report))

            # 「提出过改动」才结束漫游。被硬拦的编辑调用不算产出——它没有落到
            # 待确认清单上；把它算成产出会让漫游计数永远归零，督导就看不到
            # 「它一直在被拦」这个事实。暂存的正文同理：还没落盘就不算产出，
            # 否则督导看不到「它卡在确认那一步」。
            if edited or (
                not blocked_all
                and any(call.name in EDIT_TOOLS for call in turn.tool_calls)
            ):
                no_edit_steps = 0
                # 产出过一次，漫游就算结束了。门槛跟着复位，否则下一段漫游
                # 要等到翻倍后的那个数字才会被看见。
                roam_intervene_at = NO_EDIT_INTERVENE_AT
            elif turn.tool_calls:
                no_edit_steps += 1
                # 只读角色没有写工具，收窄无从谈起——日志说「收窄为只能写」
                # 而实际什么都没发生，那是最难查的一类假日志。
                if (
                    no_edit_steps == self.config.limit("no_edit_limit")
                    and self._commit_schema is not None
                ):
                    trace.append(
                        f"step{state.step}: 连续 {NO_EDIT_LIMIT} 步没有提出改动，"
                        "下一轮收窄为只能写"
                    )

            # 被硬拦的动作**不占步数预算**：它没有产出，也不换来新信息
            # （dogfood：两次被拒的 write_file 白吃了 2 步）。退还必须有上限，
            # 否则「不断被拦」就成了无限预算；上限也是可覆盖的标定值。
            #
            # 只退还**编辑类**的拦截。查询类的重复被拦是在打转，那正是「该停」
            # 的信号：给它退预算等于把打转延长（实测：12 条脚本的用例因此被
            # 多跑一两轮，把假模型的脚本耗尽）。护栏拦编辑是怕丢内容，
            # 拦查询是怕它原地转圈——两种拦截的后果不一样，处理也不该一样。
            refund_cap = int(self.config.limit("blocked_refund_limit"))
            blocked_edits = blocked_all and any(
                call.name in EDIT_TOOLS for call in turn.tool_calls
            )
            if blocked_edits and refunds_used < refund_cap:
                refunds_used += 1
                trace.append(
                    f"step{state.step}: 这一步的调用全部被硬拦（没有执行），"
                    f"不占步数预算（已退还 {refunds_used}/{refund_cap}）"
                )
            else:
                state.step_forward()
                _state_started = time.time()
                save_state(state, checkpoint)
                state_seconds += time.time() - _state_started

            if turn.done:
                trace.append(f"step{state.step}: 完成")
                self._archive(state, "success", trace, turn.final or "")
                clear_state(checkpoint)
                return LoopResult(
                    finished=True,
                    final=turn.final or "",
                    state=state,
                    steps=state.step,
                    resets=resets,
                    trace=trace,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model_calls=model_calls,
                    model_seconds=model_seconds,
                    tool_seconds=tool_seconds,
                    assemble_seconds=assemble_seconds,
                    verify_seconds=verify_seconds,
                    state_seconds=state_seconds,
                    supervisor_seconds=supervisor_seconds,
                    lessons_pushed=tuple(item[0] for item in pushed),
                    environment_blocked=self.environment_blocked,
                )

        self._archive(state, "fail", trace, "")
        final = "已达步数上限，任务未完成"
        if stopped_by:
            # 收尾文案必须说清**为什么**停：是钱花完了，还是督导判断做不下去。
            # 只说「未完成」的话，用户能做的只有原样再来一次。
            final = f"任务没做完，收手的原因：{stopped_by}"
        return LoopResult(
            finished=False,
            final=final,
            state=state,
            steps=state.step,
            resets=resets,
            trace=trace,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_calls=model_calls,
            model_seconds=model_seconds,
            tool_seconds=tool_seconds,
            assemble_seconds=assemble_seconds,
            verify_seconds=verify_seconds,
            state_seconds=state_seconds,
            supervisor_seconds=supervisor_seconds,
            lessons_pushed=tuple(item[0] for item in pushed),
            environment_blocked=self.environment_blocked,
        )

    def _ask_supervisor(
        self,
        state: TaskState,
        trace: list[str],
        *,
        ceiling: int,
        limit: int,
        edits: int,
        calls: int,
        resets: int,
        repeats: int,
        no_edit: int,
        tokens: int,
        asking: str,
        cycle: str = "",
        oscillations: int = 0,
        same_query: int = 0,
    ) -> Verdict | None:
        """问一次督导。关着、或者它没能给出结论，都返回 None。

        证据里刻意不含对话历史：要判断的是「这些动作有没有进展」，
        读到主循环的推理只会让它附和那个推理——和审查者必须独立同一个理由。
        它看到的是事实（记录下来的状态、发生过什么、机械统计）。

        结论以事件发出去。网页里看得到「督导说了什么」这件事很重要：
        否则一次续期在界面上和「它自己一直在跑」完全分不清。
        """
        if not self.config.supervise:
            return None
        evidence = Evidence(
            goal=state.goal,
            step=state.step,
            limit=limit,
            ceiling=ceiling,
            state=state.render(progress_hint=self.config.progress_hint),
            trace=tuple(trace),
            edits=edits,
            calls=calls,
            resets=resets,
            repeats=repeats,
            no_edit=no_edit,
            tokens=tokens,
            asking=asking,
            cycle=cycle,
            oscillations=oscillations,
            same_query=same_query,
        )
        verdict = supervise(
            self.gateway,
            evidence,
            max_grant=int(self.config.limit("supervisor_max_grant")),
        )
        if verdict is None:
            self._emit("note", {"text": "督导：没能给出结论", "ok": False})
            return None
        what = {
            "extend": f"续 {verdict.grants} 步",
            "redirect": f"改道（续 {verdict.grants} 步）",
            "stop": "建议收手",
        }.get(verdict.action, verdict.action)
        self._emit("note", {"text": f"督导：{what}——{verdict.reason}", "ok": True})
        return verdict

    def _invoke_guarded(
        self, call: ToolCall, repeats: int, seen: int = 1
    ) -> ToolResult:
        """执行一次工具调用，并对「原地打转」作出反应。

        小模型在短上下文里失去方向时会反复做同一个动作，而且不会自己停：
        实测本地 7B 在回归集里连续 10 次调用同一个 find_callers、参数一字
        不差。它不是在试探，是卡住了——这时候只能由循环把它顶回去，
        等模型自己醒悟是不现实的。

        repeats 是「连续相同的次数」，seen 是「最近几次窗口里出现的次数」。
        后者管交替打转（A、B、A、B…），前者管原地复读。

        **失败过的调用原样重提直接硬拦**（不等重复计数到 REAPEAT_BLOCK_AT）：
        dogfood 里 write_file 被拒之后，模型中间插了一次 read_file，重复计数
        归零，于是同一个必然失败的 write_file 又跑了一遍，白吃一步。参数一字
        未改的失败调用不会因为重发而改变结果，所以第一次重提就拦住，并把
        上一步的「照做的下一步」端回去。
        """
        signature = call_signature(call)
        failures = self._failed_calls.get(signature, 0)
        block_at = max(int(self.config.limit("failed_repeat_block_at")), 2)
        if failures and failures >= block_at - 1:
            return ToolResult(
                ok=False,
                content=_retry_block_message(
                    call, self._failed_output.get(signature, "")
                ),
            )
        level = max(repeats, seen)
        if level >= self.config.limit("repeat_block_at"):
            return ToolResult(
                ok=False,
                content=(
                    f"{BLOCKED_PREFIX}这个调用在最近几步里已经做过 {level - 1} 次，"
                    "结果不会改变，因此没有执行。换一种做法：换一个工具、"
                    "换一组参数，或者先查别的地方；如果上一步的输出已经够了，"
                    "就基于它往下做（提改动 / 给结论），不要再查一遍。"
                ),
            )
        result = self.registry.invoke(call)
        if not result.ok:
            self._remember_failure(signature, result.content)
        elif result.content.startswith(STAGE_MARKER):
            # 正文已暂存：这一次**不是失败**，但「同一份正文再发一遍」是这次
            # 运行里最贵的一种浪费（输出预算就是卡在这里的）。所以也记进失败表：
            # 原样重提会被硬拦，拦下的话术里带着 confirm_rewrite 的调用形状。
            self._remember_failure(signature, result.content)
        if level >= self.config.limit("repeat_warn_at"):
            if repeats >= self.config.limit("repeat_warn_at"):
                note = "注意：这一步与上一步完全相同，该调用已经执行过一次，结果不会改变。"
            else:
                note = "注意：这个调用在最近几步里已经执行过，结果不会改变。"
            return ToolResult(
                ok=result.ok,
                content=(note + "如果它没有帮你推进，就换一种做法。\n" + result.content),
            )
        return result

    def _remember_failure(self, signature: str, content: str) -> None:
        """记下一个失败过的调用（只留最近几个）。"""
        self._failed_calls[signature] = self._failed_calls.get(signature, 0) + 1
        self._failed_output[signature] = content
        while len(self._failed_calls) > _FAILED_MEMORY:
            oldest = next(iter(self._failed_calls))
            self._failed_calls.pop(oldest, None)
            self._failed_output.pop(oldest, None)

    def _clear_failures(self) -> None:
        """有改动真的落成了，就清空失败表：文件变了，旧参数可能重新有意义。"""
        self._failed_calls.clear()
        self._failed_output.clear()

    def _run_verification(self, changed: list[str] | None = None) -> tuple[str, bool]:
        """替模型跑一遍项目测试，把结果整理成它看得懂的一段话。

        验证本身出问题（命令跑不起来、超时）不算任务失败，只作为一条
        诚实的反馈告诉模型——把工具故障说成「你的代码错了」，
        会把它引到完全错误的方向上去。

        `changed` 是这一轮改过的文件：验证器据此决定**在哪个范围里验**。
        一个工作区里有很多件事时，在根上跑整条测试命令会把别人的失败报给它。
        """
        try:
            result = self.verify(changed or [])
        except Exception as exc:
            return (f"自动验证无法运行，错误类型 {type(exc).__name__}：{exc}", False)
        body = " ".join(result.content.split())[:400]
        if result.content.startswith(ENVIRONMENT_MARKER):
            # 记下来：任务没做成时，这决定要不要替它登记诊断请求。
            self.environment_blocked = True
        if result.ok:
            # 成功那句**不能请它收尾**。验证只覆盖「这次改动的地方」，
            # 任务整体做完没有它并不知道；而一句「没有别的要改就收尾」，
            # 实测在 20 道题的铺盘里做完 5 道就让它宣布完成——
            # 它把「这一批做完」当成了「任务完成」。
            return (
                f"系统自动跑了一遍测试：通过（{body}）。"
                "这说的是这次改的地方通过了。"
                "任务整体做完没有由你判断：还有未完成的接着做，"
                "确实都做完再给出结论。",
                True,
            )
        # 失败到底是什么性质，由验证器说——它才知道退出码的含义。
        # 循环这边再加一句「测试失败」会盖掉那个区别。
        return (f"自动验证的结果：{body}", False)

    @staticmethod
    def _note_progress(state: TaskState, call, result) -> None:
        """把「确实做了的事」记进状态，而不是等模型自己填。

        之前 done 一直空着——模型只填 current，从不记进度。指望它每次都
        不忘是不现实的，而**实际发生了什么，系统自己看得见**。
        所以由循环记录事实，模型只管判断（current / hypothesis / 已排除）。

        只记有产出的动作：读文件和搜索是手段而非结果，记进去只会把
        每轮都要注入的状态撑满噪声。
        """
        if call.name not in PROGRESS_TOOLS or not result.ok:
            return
        target = call.arguments.get("path") or " ".join(
            call.arguments.get("command", [])[:2]
        )
        entry = f"{call.name} {target}".strip()
        if entry not in state.done:
            state.done.append(entry)

    def _write_progress(self, state: TaskState) -> None:
        """把「做过什么」落一份在人读得懂的地方。

        状态块（每轮注入的那份）只列最近几条——它是**每个请求的税**。
        但完整记录不能因此丢掉：实测一条会话做完 20 道题，收尾时它报
        「还有 17 道没做」，因为它只看得见状态块里那 8 条。

        所以：全量留在状态里（也就落在检查点文件上），另外渲染一份
        纯文本清单，模型需要细节时可以自己读。
        """
        if not state.done:
            return
        # 会话级：`.agent/sessions/<会话>/progress.md`。写在工作区根上会让下一个
        # 会话读到上一个会话"做过什么"（真出过事，见 session_state.py）。
        path = self.config.progress_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            body = ["# 这次任务里做过的事（由循环记录，不是模型自述）", ""]
            body += [f"- {item}" for item in state.done]
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
        except OSError:
            # 记录失败不该影响任务本身：它是给人看的旁证，不是必需产物。
            pass

    def _emit(self, kind: str, data: dict) -> None:
        """向外部观察者发一条事件。

        回调异常一律吞掉：它是壳挂上来的，壳的问题不该毁掉内核的工作。
        """
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    def _archive(
        self, state: TaskState, outcome: str, trace: list[str], final: str
    ) -> None:
        """任务收尾时归档。

        归纳失败不能让用户拿不到结果——任务产出是主要价值，记忆是次要收益，
        因此这里刻意吞掉归纳异常，只保留确定性的归档动作。
        """
        if self.memory is None:
            return
        promote: list[tuple[str, str]] = []
        if self.distiller is not None:
            try:
                produced = self.distiller(state, final)
                if hasattr(produced, "entries"):
                    promote = list(produced.entries)
                    trace.append(
                        f"归纳: {produced.rounds} 轮，分治={produced.split}，"
                        f"截断={produced.truncated}，原始输出={produced.raw[:120]!r}"
                    )
                else:
                    promote = list(produced)
            except Exception as exc:
                promote = []
                trace.append(f"归纳失败（已忽略）: {type(exc).__name__}: {exc}")
        result = self.memory.archive(
            state, outcome=outcome, promote=promote, final=final
        )
        trace.append(
            f"归档: 事件#{result.episode_id}，提升 {len(result.promoted)} 条，"
            f"下沉 {len(result.demoted)} 条"
        )

