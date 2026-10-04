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
from typing import Any, Callable, Sequence

from spoolkit.agent.audit import (
    AUDIT_KIND,
    Audit,
    AuditLog,
    audit,
    boundary_write,
    load_protected,
)
from spoolkit.agent.monitor import ToolSight, inspect
from spoolkit.agent.protocol import ParseFailure, build_turn_schema, parse_turn
from spoolkit.agent.state import TaskState, clear_state, load_state, save_state
from spoolkit import limits as _limits
from spoolkit.agents.supervisor import (
    EXTEND,
    FINISH,
    STOP,
    Evidence,
    FactConflict,
    RunFacts,
    Verdict,
    supervise,
)
from spoolkit.config import Config
from spoolkit.context.assembler import Assembler
from spoolkit.context.budget import Budget
from spoolkit.context.sections import Section
from spoolkit.context import templates as T
from spoolkit.errors import ContextOverflowError, PathOutsideProjectError
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.retry import chat_with_escalation
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.llm.types import ChatRequest, Message
from spoolkit.memory.transcript import set_run_origin
from spoolkit.paths import resolve_readable
from spoolkit.prompt_sanitize import for_prompt
from spoolkit.tools.edit import (
    CONFIRM_TOOL,
    DELETE_TOOL,
    NEXT_STEP_MARKER,
    REFUSAL_MARKER,
    STAGE_MARKER,
    is_test_path,
    lingering_note,
)
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult
from spoolkit.tools.verify import ENVIRONMENT_MARKER, SCOPE_FALLBACK_MARKER, pending_renames
from spoolkit.tools.view import WorkspaceView

SYSTEM_PROMPT = T.SYSTEM

LOOKUP_TOOLS = ("find_symbol", "file_symbols", "find_callers")
EDIT_TOOLS = ("replace_lines", "replace_text", "write_file")

# 「把工作区变成我要的样子」的写工具：交内容的那几把 + 删除。
#
# 删除必须与写工具并列，否则循环看不见它：`edited` / `changed_paths` /
# 自动验证的范围都按这个集合判定，漏了它就会出现「删掉了文件，但系统认为
# 这一步什么都没做」——收尾时不报最终状态，验证也不会为它跑一次。
WRITE_TOOLS = EDIT_TOOLS + (DELETE_TOOL,)

# 「把改动推进下去」的工具：写工具 + 确认入口。
#
# 与 EDIT_TOOLS 分开的理由是它们各自回答不同的问题：EDIT_TOOLS 是**交内容**
# 的那几把（步数预算按它的产出规模放大、验证按它改过的路径分派），而确认入口
# 不交内容、只是一个很小的举证回合。但有一处它必须算进去：漫游太久时循环会把
# 采样空间收窄成「只能写」——收窄之后如果确认入口被语法拿掉，模型就再也
# 确认不了那份暂存的正文了。
COMMIT_TOOLS = WRITE_TOOLS + (CONFIRM_TOOL,)

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

# 只看不写**不等于**打转：先读后计划本来是许多任务的第一步（「优化一下旧文件」
# 就属于这种）。所以到干预线时先回头看这几步有没有**拿到新信息**——有任何
# 一次成功的查看，就按正常探索处理。1 = 开，0 = 退回「只看不写即打转」。
NO_EDIT_EXPLORE_GRACE = bool(int(_limits.knob("no_edit_explore_grace").default))
# 往回看几步。它与 RECENT_WINDOW 同量级，但不是同一个东西：那个管
# 「同一个调用出现几次」，这个管「这几步里有没有新信息进来」。
NO_EDIT_LOOK_WINDOW = int(_limits.knob("no_edit_look_window").default)

# 漫游到这个步数还没产出，就升级给督导。
#
# NO_EDIT_LIMIT 那一步做的是**收窄输出通道**（只能写），它盖得住「它还想查」
# 这一种。盖不住的是「收窄之后它去翻别的文件」——每一步换一个查询，
# 机械计数器永远归零。到这一步该有人看看它到底在干什么，而不是继续加规则。
#
# 判据已经按任务形状区分：**同一个失败动作反复重试**才算打转（见
# failed_repeat_at 那条升级路径），而「读了很多步、每一步都拿到新东西」
# 只在超过本条阈值时交给督导**问一句**（措辞是「它还在探索」，不是
# 「它在打转」）——两者给督导的问题不一样，结论也就不一样。
NO_EDIT_INTERVENE_AT = int(_limits.knob("no_edit_intervene_at").default)

# 同一个动作**连续失败**几次就升级给督导。
#
# 这是「后一种才算打转」那条判据的机械阈值：参数一字不改的重提由
# failed_repeat_block_at 硬拦（护栏层），而「同一个坑连着踩」是**要给判断**
# 的事——拦住了动作，但拦不住它想干那件事。把它单独说出来，督导看到的
# 就是「同一个失败动作反复重试」，而不是「它好几步没写代码」这种含糊信号。
FAILED_REPEAT_AT = int(_limits.knob("failed_repeat_at").default)

# 硬拦（没有执行）时**退还步数预算**的工具类别。
#
# 查询类的硬拦原先不退：理由是「查询被拦是在打转，退预算等于延长打转」。
# 那条理由对「同一查询重发」成立，对「同一个失败动作重试」不成立——后者
# 的出路是换做法，而不是让它把预算花在复读上。dogfood 那条轨迹里它第 3 步
# 原样重提 survey、被拦、照样吃掉一步（4 步预算是上限，再撞一次就收手）。
# 编辑类退还一直是对的（dogfood：两次被拒的 write_file 白吃 2 步），
# 这里只是把同一条账算到查询类上。
_REFUND_TOOLS = WRITE_TOOLS + (CONFIRM_TOOL, "read_file", "list_dir", "search_code", "survey")

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

    **意图不进指纹**（虽然它是「同一次调用」的一部分）。理由是一次实测：
    意图不可靠时会读不到，而退路是这一轮的 `thought`——那个字段几乎每轮
    都不同。把它算进指纹，等于让模型**换一句说法就绕过重复检测**，
    护栏当场失效（test_repeat_guard 那几条会变成绿的反面）。要看「它换个
    说法又试了一遍」，看的是 trace 里那两行「意图:」，不是指纹。
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

# 「刷新读取」话术的前缀。
#
# 这一轮的核心修法是：重复调用**不再只看签名**，还要看两件事实——上一份结果
# 是不是还在上下文里、文件是不是变过。两者任一不成立时就必须能再取一次；
# 取的时候用这个前缀说明「为什么这次允许重复」，让调度日志里看得见。
# 它**不是**被拦：调用照常执行，只是把「上一次的内容已经不在了」这个事实
# 写进 trace 与事件流。没有这一行，一个合规的重复读在日志里和原地打转
# 长得一模一样，调这块时只能靠猜。
GUARD_REFRESH_PREFIX = "⟳ 刷新读取："

# 允许「结果已被压掉」之后重新读，是为了不让模型被护栏饿死。但**不能无限**：
# 没有上限的话，它只要每一步都触发一次硬拦（硬拦还退还步数预算），就能把
# 预算变成无限。上限与 blocked_refund_limit 同一口径，只放行前几次。
_REFRESH_LIMIT = 3


def _content_fingerprint(text: str) -> str:
    """一段内容的指纹：sha1 前 12 位 + 行数。

    行数不参与哈希，是要让它**同时**回答两个问题：内容变没变、行号漂没漂。
    只给哈希的话，「文件变了 3 行」这件事在读回里看不见，而它正是
    replace_lines 出错的原因。
    """
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{digest}/{len(text.splitlines())}行"


def _is_blocked(result: ToolResult) -> bool:
    """这次失败是「被拦下没执行」，而不是「执行了但失败」。"""
    return (not result.ok) and result.content.startswith(_BLOCKED_MARKS)


def still_exploring(look_window: list[bool]) -> bool:
    """只看不写的那几步，是不是**每一步**都拿到了新东西。

    写成独立函数是因为这里有一个很容易写反的判据，而且反了不会有任何报错，
    只会让「先读后计划」这个说法被用在一段其实在空转的窗口上（督导据此续期，
    预算就投进了一个什么都没看到的循环）。判据是 `all` 而不是 `any`：
    窗口里夹着一次落空，那一次落空没有被任何东西补偿——后一步的成功是自己
    的收获，不是前一步的。

    空窗口返回 False：还没看过任何东西时，说「它在探索」没有依据。
    """
    if not NO_EDIT_EXPLORE_GRACE:
        return False
    return bool(look_window) and all(look_window)


def _intent_clause(call: ToolCall) -> str:
    """把「你像是要做什么」这句话写出来。

    失败话术里必须先回应意图：只说「用 read_file」时，一个真正想做别的事
    （比如「这个文件在哪儿」）的模型会把那句话判成「不适用于我」，然后原样
    重试——dogfood 里 survey 一个文件连撞两次就是这么来的。回应了意图之后，
    它能从「哪一条是在说我的目的」里认出自己该走哪条路。

    口径与 SYSTEM 里那句 why 要求**同一套**（用户第 3 条质疑）：想拿到什么 +
    拿到之后接下来怎么做。两处不一的话，模型会照提示写，而失败话术里回显的
    是另一个口径——它会以为系统在说别人的意图。
    """
    if not call.intent:
        return ""
    return (
        f"你写的意图是「{call.intent}」"
        "（口径：想拿到什么 + 拿到之后接下来怎么做）。"
    )


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
    intent = _intent_clause(call)
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
    if intent:
        # 意图单独一段：它回答的是「你为什么又发一遍」，而不是「错在哪」。
        parts.insert(
            1,
            intent
            + "这个理由上一次也已经试过了；同样的意图换个说法重发，"
            "拿到的还是同一个结果。"
            "下一步要能被看见：写明「这一次想拿到什么、拿到之后做什么」。",
        )
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
        "end_line 大于或等于 start_line，且不得超过文件总行数；"
        "还必须带 expect——被替换区间原来的内容，从 read_file 的结果里逐字抄。"
        "锚点对不上说明行号漂了（上一次编辑之后行数会变），"
        "重新 read_file 再抄一遍，或者改用 replace_text。\n"
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
        if registry.get(DELETE_TOOL) is not None:
            # 紧跟改动方式：删除与写、改并列，是「把工作区变成我要的样子」
            # 的第三种动作。没注册就一个字都不提——提到一把没有的工具，
            # 等于给模型一条它做不到的指令。
            lines.append(T.WORKFLOW_DELETE)
        # 改名是「改动」的一个特例，判据却跨出被改的文件本身，所以单列一条。
        # 这个分支本来就在「有写工具」里面，只读角色看不到它。
        lines.append(T.WORKFLOW_RENAME)

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
    # 新增的两段（删除、跨文件改名）常驻，是每个请求都要付的税。实测它们把
    # system 区段顶过了 900 token 的绝对上界，于是 4096 窗口下报「必留区段
    # system 超出有效预算」——所以 SYSTEM 与 WORKFLOW_* 同步做了等量压缩。
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
PROGRESS_TOOLS = ("write_file", DELETE_TOOL, "replace_lines", "run_command")
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
        pending: Any | None = None,
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
        # 待确认改动。护栏要用它算「这个文件现在长什么样」——`read_file` 看到的是
        # 叠加了 pending 之后的视图，而磁盘上是旧内容。拿磁盘去比会把「文件没变」
        # 判反，于是该放行的刷新读取被拦下。
        self.pending = pending
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
        # ---- 重复调用护栏的两个判据（见 _invoke_guarded）----------------------
        # ① 上一份结果还在不在上下文里：装配时的历史版本号。
        self._context_revision = 0
        self._seen_at_revision: dict[str, int] = {}
        # ② 文件变没变：路径 → 读它时的内容指纹（叠加 pending 之后的视图）。
        self._read_fingerprints: dict[str, str] = {}
        # 放行过的刷新读取次数（有上限，见 _REFRESH_LIMIT）。
        self._refreshes_used = 0
        # 最近一次自动验证的结论与它覆盖过的路径。收尾时要报「最终文件的验证
        # 结果」，而刚刚验过**同一批内容**时不该再跑一遍——验证是起子进程，
        # 重复跑只是让运行更慢，结论一模一样（见 _with_final_state）。
        #
        # 缓存键是 `(路径 → 内容指纹)`，**不是路径集合**。这是实测抓出来的
        # bug：改名那条探针里，中途那一步验的是「2 处改动、导入已经断了」的
        # 版本（结论是不通过），而收尾时 4 处改动都已落盘、模型自己的
        # run_command 与判据跑测试都是 0；可 `set(changed) <= self._verified_paths`
        # 只看路径——路径没变，于是那份**失败原文**被原样搬进了收尾报告，
        # 结论与文件真实状态相反。内容一变就必须重新验证。
        self._last_verify: tuple[str, bool] | None = None
        # 上一次验证覆盖过的**内容键**（路径 + 内容指纹）。与 _last_verify
        # 同生共死：结论和「它是对哪一份内容下的」必须成对存在。
        self._last_verify_key: str = ""
        # 自动审查层 L1 的账本（见 spoolkit.agent.audit）。每次 run() 开始时清空：
        # 它是「本次运行」的发现，不是跨任务的记忆。
        self.audit_log = AuditLog()
        # 最近一次验证**新加**的那几条发现。事件流按它发 note，
        # 而不是把整本账每步重发一遍（重发等于给上下文和日志灌噪声）。
        self._last_findings: tuple = ()
        # 本次运行里每个改动文件**磁盘上的原始行数**。判「迎合测试」要用它：
        # 行数是可证伪的证据，比措辞可靠（测试失败的同一步，业务代码变短了）。
        self._base_lines: dict[str, int] = {}
        # 本轮的 trace 引用。辅助方法（护栏的放行理由）要往它上面写一行，
        # 又不能把 trace 当参数一路传下去。
        self._trace: list[str] | None = None
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

        **注入型区段统一过 `for_prompt`**：预取、热记忆、教训、督导反馈都是
        「从别处取回来、直接摆到模型面前」的文本，里面同样不许带本次运行专属的
        临时/随机路径（同一份内容换个随机副本名重跑，提示必须逐字相同）。
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
            sections.append(
                Section(name="hot_memory", text=for_prompt(hot), priority=20)
            )
        if lesson_text:
            sections.append(
                Section(name="lessons", text=for_prompt(lesson_text), priority=25)
            )
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
            sections.append(
                Section(name="retrieval", text=for_prompt(prefetched), priority=35)
            )
        if feedback:
            # **必留区段**，而不是与预取共用 retrieval 配额。
            #
            # 原先它排在 retrieval（0.15，本机 8K 窗口下约 1044 token）里跟
            # 预取抢位置：一段督导建议加「请用 assessment 表态」那三选一，
            # 一旦超配额就会被 `_fit` 从**尾部**裁掉——而尾部正是那句表态
            # 要求。被裁掉不会报错，只会安静地退化成「督导下命令、模型照做」，
            # 也就是这一次要修的那个毛病。实测两处合起来的量很小（一段
            # message 加 60 余字），必留没有成本，但换来「这段话一定完整送到」。
            sections.append(
                Section(
                    name="feedback",
                    text=for_prompt(feedback),
                    priority=40,
                    mandatory=True,
                )
            )
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
        # 本次运行的时间起点：注入到上下文里的时间一律按它归一化
        # （`memory/transcript.prompt_stamp`）。不标定的话，同一份内容在
        # 不同时刻重跑会给出不同的提示——一次 t=0 复跑里，`search_history`
        # 输出里那 6 分钟的差被放大成了两条完全不同的轨迹。
        set_run_origin()
        checkpoint = self.config.task_path(task_id)

        # 恢复未完成的检查点。之前这里只写不读，等于「崩溃可恢复」这句
        # 承诺从未兑现——跑到一半中断，下次只能从零开始。
        state = load_state(checkpoint) if resume else None
        resumed = state is not None
        if state is None:
            state = TaskState(task_id=task_id, goal=goal)
        goal = state.goal
        # 「上次建议 + 它的表态」是**本次会话内**的对照，不是任务状态：
        # 续跑时读到上一段留下的表态，等于让新的一次运行替上一次认账。
        state.assessment = ""
        state.suggestion = ""

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
        # 本轮运行重置护栏状态：上一次 run() 的读取指纹与上下文版本不能带过来
        # （带了的话，第一次重复读就会拿着旧 run 的文件状态做判断）。
        self._trace = trace
        self._context_revision = 0
        self._seen_at_revision.clear()
        self._read_fingerprints.clear()
        self._refreshes_used = 0
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
        # 「只看不写而且什么都没看到」的连续步数。它与 no_edit_steps 分开数，
        # 理由正是用户那条质疑：「优化一个旧文件」本来就要先读、再计划，
        # **读了东西**的步数与**白看**的步数不该进同一个计数器。
        no_progress_steps = 0
        # 「同一个失败动作反复重试」的连续计数：任何一次失败 +1，任何一次成功
        # 清零。它与 edit_fail_streak 不是一回事——后者只数写工具（用来决定
        # 什么时候给格式提示），这个数**所有**工具，用来决定什么时候该问督导
        # 「这是不是同一个坑里反复试」。
        failed_streak = 0
        # 最近几步里有没有拿到新信息：每次结果**成功**记 True，失败记 False。
        # 只看不写时用它区分「正常探索」与「连读都读不到东西」。
        look_results: list[bool] = []
        # 上一次督导建议的原文，与它对那条建议的表态。两者都要送到下一轮
        # 督导的证据里——否则督导只能看到「它没照做」，看不到为什么。
        last_suggestion = ""
        last_assessment = ""
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
        failed_repeat_at = int(self.config.limit("failed_repeat_at"))
        edits_made = 0
        tool_calls_made = 0
        # 这次运行真正被改动过的路径。督导的事实性断言要拿它核对
        # （「0 处改动」这种话不能端给模型），收手时的最终验证也要用它定范围。
        changed_paths: list[str] = []
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
            # 「先读后计划」不是打转：只看不写到干预线时，先回头看这几步
            # 有没有**拿到新信息**。全都有，就是正常探索——照样交给督导，
            # 但给它的**问题**不同（「它还在探索，判断该不该继续」），
            # 于是它不会像原来那样一看到「几步没产出」就建议收手。
            look_window = look_results[-NO_EDIT_LOOK_WINDOW:]
            exploring = still_exploring(look_window)
            # 反过来，「看了好几步却一步新信息都没进来」才是那类该被看见的
            # 漫游：它每一步都在换查询，机械计数器永远归零。这条与
            # no_edit_limit 的分工是：限值**收窄输出通道**（语法层，不花钱），
            # 这条**叫督导**（花一次调用，但能给出「换个做法」的具体建议）。
            stuck_on_roam = (
                self.config.supervise
                and no_progress_steps >= roam_intervene_at
            )
            stuck_on_explore = (
                self.config.supervise
                and exploring
                and no_edit_steps >= roam_intervene_at
            )
            # 同一个动作连续失败：机械层已经把原样重提拦下了（动作没执行），
            # 但「它想干那件事」这件事没被拦下。这是**要判断**的点，
            # 也正是用户说的「同一失败动作反复重试才算打转」。
            stuck_on_failed_repeat = (
                self.config.supervise
                and failed_repeat_at > 0
                and failed_streak >= failed_repeat_at
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
                or stuck_on_explore
                or stuck_on_cycle
                or stuck_on_oscillation
                or stuck_on_query
                or stuck_on_failed_repeat
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
                elif stuck_on_failed_repeat:
                    asking = (
                        f"同一个动作已经连续失败 {failed_streak} 次，"
                        "每次都在同一个坑里换说法重试"
                    )
                elif stuck_on_roam:
                    asking = (
                        f"它已经连续 {no_progress_steps} 步只看不写，"
                        "而且这几步里一次都没有拿到新内容（查询都在失败或落空）"
                    )
                elif stuck_on_explore:
                    # 只改「问题」不改「阈值」：交给督导判断，但把事实说准。
                    # 这一步是用户第二条质疑的落点——「只看不写」本身不是
                    # 打转的证据，先读后计划本来就要看几步。
                    asking = (
                        f"它已经连续 {no_edit_steps} 步只看不写，"
                        f"但最近 {len(look_window)} 步每一步都拿到了新内容，"
                        "没有任何失败动作被反复重试（像是先读后计划）"
                    )
                else:
                    asking = (
                        f"它已经连续 {repeats} 次调用同一个工具，参数也相同，仍在重复"
                    )
                if exploring and stuck_on_explore:
                    trace.append(
                        f"step{state.step}: 这一步按**正常探索**交给督导"
                        "（读到了新内容，不是打转）"
                    )
                # 把「为什么把它叫起来」记下来：调这块时唯一能看的东西就是它。
                trace.append(f"step{state.step}: 叫督导——{asking}")
                # 上一轮注入过的建议要一起送去：督导看不到「它上次怎么答的」，
                # 就只能一遍遍重复同一个建议，最后从建议滑向命令。
                state.suggestion = last_suggestion
                state.assessment = last_assessment
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
                    assessment=last_assessment,
                    suggestion=last_suggestion,
                    changed=tuple(changed_paths),
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
                        # 退避按「这次是被哪个信号叫起来的」算：白看那条用
                        # no_progress_steps，探索那条用 no_edit_steps。
                        # 混用会让另一个信号的门槛莫名其妙地翻倍。
                        (no_progress_steps if not exploring else no_edit_steps) * 2,
                        roam_intervene_at * 2,
                    )
                    cycle_at = max(cycle_streak * 2, cycle_at * 2)
                    cycle_streak = 0
                    oscillation_at = max(
                        report.oscillations * 2, oscillation_at * 2
                    )
                    same_query_at = max(
                        report.same_query * 2, same_query_at * 2
                    )
                    failed_repeat_at = max(
                        failed_streak * 2, failed_repeat_at * 2
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
                    # 说「做完了」之前也要对最终文件跑一次验证：这是用户点名的
                    # 第 7 条（收手与落盘抢跑）。已经落成（进待确认清单）的改动
                    # 是真实产出，结论里必须带上它的实际状态。
                    done_text = self._with_final_state(
                        done_text, state, trace, changed_paths
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
                        # 记下「我们转给它的那段原话」：下一次叫督导时要
                        # 连它的表态一起送过去，督导才知道自己的建议被怎么答的。
                        last_suggestion = verdict.message
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
                            # 与上面那条退避同一个口径：按被叫起来的信号算。
                            (no_progress_steps if not exploring else no_edit_steps) * 2,
                            roam_intervene_at * 2,
                        )
                        cycle_at = max(cycle_streak * 2, cycle_at * 2)
                        cycle_streak = 0
                        oscillation_at = max(
                            report.oscillations * 2, oscillation_at * 2
                        )
                        same_query_at = max(
                            report.same_query * 2, same_query_at * 2
                        )
                        failed_repeat_at = max(
                            failed_streak * 2, failed_repeat_at * 2
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
            #
            # **后面必须缀上「请用 assessment 表态」那一段**：没有它，督导的
            # 话就只是一段会被照做的命令（dogfood 里「建议收手」下去，模型就
            # 收手了）。要求表态之后，接受与不接受都会留下理由，而那段理由
            # 会进 trace、进状态块、进下一次督导的证据——「它到底为什么那样做」
            # 这件事才第一次可查。
            if redirect is not None:
                feedback = f"{redirect}\n\n{T.SUPERVISOR_ASK}"
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
                # 历史被整个丢掉：此前所有工具结果的**内容**都不在上下文里了。
                # 版本号 +1，重复调用护栏据此放行刷新读取（见 _refresh_reason）。
                self._context_revision += 1
                trace.append(f"step{state.step}: 上下文重置（第 {resets} 次）")
                _assemble_started = time.time()
                assembled = self._assemble(
                    state, history, feedback, prefetched, hot, lesson_text
                )
                assemble_seconds += time.time() - _assemble_started
            elif assembled.demand_tokens >= self._budget.soft_limit():
                history = history[-(MAX_RECENT_TURNS // 2):]
                self._context_revision += 1
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
                # 同上：历史被丢掉，工具结果的内容都不在上下文里了。
                self._context_revision += 1
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
            # 它对督导建议的表态。记下来的理由与记意图一样：**没有它，
            # 「它为什么没照做」无从查起**，而下一个结论就会从建议滑向命令。
            # 空串不记——「没有督导建议要回应」和「它拒绝回应」是两件事，
            # 混在一起会让日志说谎。
            if turn.assessment:
                last_assessment = turn.assessment
                trace.append(
                    f"step{state.step}: 它对此前督导建议的表态："
                    f"{' '.join(turn.assessment.split())[:200]}"
                )
                if self.on_event is not None:
                    # 单独一类事件：网页壳/桥要能把它显示成「Agent 回应了督导」，
                    # 而不是混在工具流水里。`kind` 放在 data 里，`--events`
                    # 打出来就是一行 note，text 里能直接读到它的表态原文。
                    self._emit(
                        "note",
                        {
                            "text": f"Agent 表态：{turn.assessment}",
                            "ok": True,
                            "kind": "assessment",
                        },
                    )
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
                    # 这次调用发生在哪个上下文版本里。重复调用护栏用它判断
                    # 「上一份结果还在不在上下文里」——被砍过历史之后，
                    # 同一个签名虽然还算「重复」，内容却已经不在了。
                    self._seen_at_revision[signature] = self._context_revision
                    blocked = _is_blocked(result)
                    if not blocked:
                        blocked_all = False
                    # 「同一个失败动作反复重试」判据的原料，以及「这几步有没有
                    # 拿到新信息」的原料。被硬拦的那次同样算一次失败：它没有
                    # 换来任何新东西，那正是「在同一个坑里」的证据。
                    if result.ok:
                        failed_streak = 0
                    else:
                        failed_streak += 1
                    look_results.append(result.ok)
                    if NO_EDIT_LOOK_WINDOW > 0:
                        del look_results[:-NO_EDIT_LOOK_WINDOW]
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
                            # 本次运行**累计**改过哪些文件：结尾的最终验证与
                            # 结论里的真实状态都用它（`changed` 只覆盖这一轮）。
                            # 两个出口各记一份，不是重复——一个按轮、一个按次运行。
                            changed_paths.append(path)
                            # 这个文件**第一次被改之前**磁盘上的行数：审查层判
                            # 「迎合测试」（测试没过而业务代码变短）要用它。
                            if path not in self._base_lines:
                                self._base_lines[path] = self._disk_lines(path)
                            # 这个文件改过了：旧的行号与旧的读取指纹都作废。
                            # 清掉之后，同一个 read_file 的重提会被放行成
                            # 「刷新读取」，而不是被硬拦在一份过期内容上。
                            self._forget_read(path)
                            # 越界写闸门：这次改动落在受保护路径上就当场报一条
                            # 高严重度发现（事件流 + trace + 收尾报告都看得见，
                            # **不阻断**）。现场见 agent/audit.boundary_write。
                            self._note_boundary_write(path)
                    elif not result.ok and call.name in COMMIT_TOOLS and not blocked:
                        # 被硬拦的不计进「连续失败」：它没执行，也不是格式错误，
                        # 而且拦它的话术本身已经给了照做的下一步。
                        edit_fail_streak += 1
                    if level >= self.config.limit("repeat_warn_at"):
                        trace.append(
                            f"step{state.step}: 重复调用第 {level} 次：{call.name}"
                        )
                    repeats = level
                    # 改完当场查「这次改名有没有在别处留下旧名」。报告跟着工具
                    # 输出回到模型面前，而不是等它自己想起来去全项目搜一遍——
                    # 实测它不会搜，它会在四个 .py 改完之后直接宣称「已统一」，
                    # 而 README / docs 里的旧名一个字没动（见 edit.lingering_note）。
                    extra = self._lingering_text(call, result)
                    payload = {
                        "name": call.name,
                        "ok": result.ok,
                        # 残留报告提到**前面**：事件里的 detail 会被截到 200 字，
                        # 而写在末尾的那一段就永远读不到（实测：调试时我从事件流
                        # 里搜「旧名仍有残留」搜不到，一度以为功能没生效）。
                        # 「有没有报出残留」是该被机械核对的事实，放在第一位。
                        "detail": " ".join(f"{extra} {result.content}".split())[:200]
                        if extra
                        else " ".join(result.content.split())[:200],
                    }
                    # 意图进事件流。**这是「工具调用带上意图」这件事的出口**：
                    # `--events` 录下来的 JSONL 里每一行 tool 事件都能读到
                    # 「它这次想干什么」，调工具与调提示词时才有依据分清
                    # 「工具不完善」和「它理解错了」。没有这一项，同一个失败
                    # 在日志里只有工具名与参数，意图只能靠人去猜。
                    if call.intent:
                        payload["intent"] = call.intent[:200]
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
                        # **回灌边界的兜底口**：所有工具输出都从这里进提示，所以
                        # 这里过一遍 `for_prompt`（本次运行专属的临时/随机路径换
                        # 成占位符）。放在装配这一步而不是各个工具里，是因为工具
                        # 的输出同时还要给人看——事件流的 `detail`、trace、落盘
                        # 日志都用上面那份**未清理**的原文，精确路径仍可查。
                        outputs.append(
                            f"[{call.name}] {status}: {for_prompt(result.content)}"
                        )
                    if extra:
                        outputs.append(for_prompt(extra))
                        trace.append(
                            f"step{state.step}: {extra.splitlines()[0]}"
                        )
                    # 失败时把输出压成一行摘要。取第一行不行：那里是命令本身，
                    # 而不是错误原因——调试时会被误导。
                    # 失败信息给足长度：160 字刚好够看到命令本身，
                    # 而真正的原因在后面——调试时会被自己的截断挡住。
                    flat = " ".join(result.content.split())
                    detail = "" if result.ok else f": {flat[:400]}"
                    # 意图进 trace（督导看到的正是它）。一行里同时给出
                    # 「想干什么」与「结果如何」，督导才不必从工具名去猜目的。
                    why = (
                        f"（意图: {' '.join(call.intent.split())[:120]}）"
                        if call.intent
                        else "（它没有写意图）"
                    )
                    trace.append(
                        f"step{state.step}: 工具 {call.name}{why} -> {status}{detail}"
                    )
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
                    # 验证对象是**本次运行累计落盘 + 待确认的全部改动**，不是
                    # 这一步的半成品：多文件改名/搬移天生多步，对半新半旧的树
                    # 逐步验证必然失败，而那个失败必然诱发迎合测试的改动
                    # （rename-cross-file 复跑实测：模型为此删掉了表头行）。
                    for _path in changed:
                        if _path not in changed_paths:
                            changed_paths.append(_path)
                    scope_paths = list(dict.fromkeys([*changed, *changed_paths]))
                    report, passed = self._run_verification(scope_paths)
                    # 验证单独计一笔：它是**起子进程跑测试**，优化方向
                    # （换更快的测试、只跑相关文件）与工具那条完全不同。
                    verify_seconds += time.time() - _verify_started
                    trace.append(
                        f"step{state.step}: 自动验证 -> {'通过' if passed else '失败'}"
                        f"（累计 {len(scope_paths)} 处改动，"
                        f"{time.time() - started:.1f}s）"
                    )
                    self._emit(
                        "tool",
                        {
                            "name": "自动验证",
                            "ok": passed,
                            "detail": " ".join(report.split())[:200],
                        },
                    )
                    # 发现（回退 / 中间态 / 迎合测试）单独走一类 note 事件：
                    # 「高危信号出现过几次」要能机械地数出来，不能靠读自然语言。
                    # `_run_verification` 会把**这一次新加的那几条**放在
                    # `self._last_findings` 里，这里只负责发出去。
                    for finding in self._last_findings:
                        self._emit(
                            "note",
                            {
                                "text": finding.render(),
                                "ok": True,
                                "kind": AUDIT_KIND,
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
                no_progress_steps = 0
                # 拿到过产出，窗口清零：留着上一步的成功查看，会让「正在推进」
                # 那一侧永远为真（旧的成功记录不该替新的一步作证）。
                look_results.clear()
                # 产出过一次，漫游就算结束了。门槛跟着复位，否则下一段漫游
                # 要等到翻倍后的那个数字才会被看见。
                roam_intervene_at = NO_EDIT_INTERVENE_AT
                # 「同一个失败动作反复试」这一段也结束了：失败计数从零起算，
                # 否则下一段里第一次失败就会立刻越线（9 成是误报）。
                failed_repeat_at = FAILED_REPEAT_AT
                failed_streak = 0
            elif turn.tool_calls:
                no_edit_steps += 1
                # 这一步有没有拿到新信息。窗口里全是失败 = 它换着花样
                # 也什么都没看到，那才是该被督导看见的那种漫游。
                if not any(look_results[-NO_EDIT_LOOK_WINDOW:]):
                    no_progress_steps += 1
                else:
                    no_progress_steps = 0
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
            # 退还范围包括**查看类**（read_file / list_dir / search_code / survey）。
            # 原先只退编辑类，理由写的是「查询被拦是在打转，退预算等于延长打转」；
            # 那条理由对**同一查询重发**成立，对**同一失败动作重试**不成立——
            # 后者的出路是换个做法，而不是让它把预算花在复读上。用户那条轨迹里
            # 第 3 步原样重提 survey 被拦、照样吃掉一步，4 步预算直接见底、
            # 来不及做任何改动。退还仍有上限，所以「靠被拦换无限预算」不成立。
            refund_cap = int(self.config.limit("blocked_refund_limit"))
            blocked_repeat = blocked_all and any(
                call.name in _REFUND_TOOLS for call in turn.tool_calls
            )
            if blocked_repeat and refunds_used < refund_cap:
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
                # 它自己宣告完成时也一样要报最终状态：用户点名的第 7 条
                # （收手与落盘抢跑）不区分「谁收的手」。已经落成的改动、
                # 最终文件的真实行数与指纹，都附在结论后面。
                final_text = self._with_final_state(
                    turn.final or "", state, trace, changed_paths
                )
                self._archive(state, "success", trace, turn.final or "")
                clear_state(checkpoint)
                return LoopResult(
                    finished=True,
                    final=final_text,
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
        # **收手也要报最终状态**（用户点名的第 7 条）。收手判词与落盘是同一次
        # 运行里的两件事，而旧行为里判词说「0 处改动」时，改动可能刚刚落成、
        # 甚至已经写进用户的工作区（`--policy auto`）。所以这里对最终文件跑
        # 一次验证并把真实结果附在结论后面：已经落成的改动不许被判词抹掉，
        # 文件变好了也要如实说。
        final = self._with_final_state(final, state, trace, changed_paths)
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

    def _content_key(self, changed: Sequence[str]) -> str | None:
        """这批改动**当前内容**的缓存键：`路径=内容指纹` 排序后拼起来。

        为什么不是路径集合：路径集合只回答「验的是不是同一批文件」，而
        「同一批文件」在改名途中可以先是坏的、后被改好——沿用旧的结论就会
        把一份**过期的失败原文**搬进收尾报告，与文件真实状态相反。
        内容一变，键就变，收尾必然重新验证。
        """
        view = WorkspaceView(self.config.project_root, self.pending)
        parts: list[str] = []
        for path in sorted(set(changed)):
            try:
                target = resolve_readable(
                    self.config.project_root, path, self.read_roots
                )
                if not view.exists(target):
                    # 「现在不存在」也是一个必须有别于其它状态的内容事实：
                    # 拿空串冒充它，会把「刚删掉」与「刚写成空文件」混成一条。
                    parts.append(f"{path}=<不存在>")
                    continue
                text = view.read_text(target)
            except (PathOutsideProjectError, OSError, UnicodeDecodeError):
                parts.append(f"{path}=<读不到>")
                continue
            parts.append(f"{path}={_content_fingerprint(text)}")
        if not parts:
            return None
        return "\n".join(parts)

    def _file_state_line(self, changed: Sequence[str]) -> str:
        """最终文件状态：哪些文件、现在多少行、指纹是什么。

        走的还是 `WorkspaceView`（叠加待确认改动）——模型看到的、验证器验的、
        这里报的必须是同一份内容。落盘之前也报得出真实状态，因为改动就在
        待确认清单里。
        """
        if not changed:
            return "最终文件状态：这次运行没有改过任何文件。"
        view = WorkspaceView(self.config.project_root, self.pending)
        parts: list[str] = []
        for path in changed:
            try:
                target = resolve_readable(
                    self.config.project_root, path, self.read_roots
                )
                if not view.exists(target):
                    parts.append(f"{path}（现在不存在）")
                    continue
                text = view.read_text(target)
            except (PathOutsideProjectError, OSError, UnicodeDecodeError):
                parts.append(f"{path}（读不到）")
                continue
            parts.append(
                f"{path}：{len(text.splitlines())} 行，指纹 {_content_fingerprint(text)}"
            )
        return "最终文件状态：" + "；".join(parts) + "。"

    def _with_final_state(
        self,
        final: str,
        state: TaskState,
        trace: list[str],
        changed: Sequence[str],
    ) -> str:
        """给收尾结论补上「最终文件真实验证结果 + 真实状态」。

        收手时也要对最终文件跑一次验证（有验证器时）。结果**如实**附在后面：

        - 改动确实落成了 → 「改动已落成」+ 验证结果，不许说成「0 处改动」；
        - 文件在收手时是坏的 → 说出来；收手判词不改写这件事。

        **一处改动都没有时不加这段**。理由有两条，都不是省事：① 「这次运行
        没改过任何文件」对一个只读角色（审查者）是纯粹的噪声，每一句结论后面
        都拖着它；② 更要紧的是它会被读成「系统在替我下结论」。真正要防的是
        「有落盘却被判词抹掉」，而那种情况必然有改动——所以只在有改动时段。
        """
        if not changed:
            return final
        facts = self._file_state_line(changed)
        # 越界写那类高危发现要进收尾报告：它回答的是「这次运行动了不该动的
        # 东西没有」，而收尾报告是人真的会读的那一份文本（见 `_boundary_report`）。
        boundary = self._boundary_report()
        # 刚刚验过的**就是同一批内容**时不再起第二次进程：验证是起子进程跑
        # 测试，重复一遍只是让运行更慢，结论一字不差。要求是「收手时报出真实
        # 的最终验证结果」，不是「每收一次手就多跑一次测试」。
        #
        # **判据是内容指纹，不是路径集合**（这是实测抓出来的 bug）：路径集合
        # 会命中「中途那一版」，而中途那一版可能正是坏的——改名那条探针里
        # 验的是「2 处改动、导入已断」的版本，收尾时 4 处都已落盘、都是绿的，
        # 却把那份失败原文原样搬进了报告。内容变了就必须重新验证一次。
        key = self._content_key(changed)
        if self.verify is not None and key is not None and key == self._last_verify_key:
            body, passed = (
                self._last_verify if self._last_verify else ("（无记录）", False)
            )
            outcome = (
                ("最终验证：通过。" if passed else "最终验证：未通过。")
                + "（上一步验过之后这些文件的内容一字未变，"
                "所以沿用那一次的结果）"
                + body
            )
            trace.append(
                f"收尾: 报出最终文件的验证结果 -> {'通过' if passed else '未通过'}"
                "（内容未变，沿用最近一次）"
            )
            self._emit(
                "tool",
                {
                    "name": "最终验证",
                    "ok": passed,
                    "detail": " ".join(body.split())[:200],
                },
            )
            return f"{final}\n本次运行共落成 {len(changed)} 处改动。{facts} {outcome}{boundary}"
        if self.verify is not None:
            report, passed = self._run_verification(list(changed))
            outcome = ("最终验证：通过。" if passed else "最终验证：未通过。") + report
            trace.append(
                f"收尾: 对最终文件跑了一次验证 -> {'通过' if passed else '未通过'}"
            )
            self._emit(
                "tool",
                {
                    "name": "最终验证",
                    "ok": passed,
                    "detail": " ".join(report.split())[:200],
                },
            )
        else:
            outcome = "最终验证：本次运行没有挂验证器，未跑。"
        landed = f"本次运行共落成 {len(changed)} 处改动"
        return f"{final}\n{landed}。{facts} {outcome}{boundary}"

    def _run_facts(
        self, *, edits: int = 0, changed: tuple[str, ...] = ()
    ) -> RunFacts:
        """交给督导断言核对用的运行记录。

        三个来源都是机械的：工具名取自注册表，改动与路径取自循环自己的账。
        **不用 trace 里的字**——那正是不能拿来当事实的东西（督导的错话就是
        从那些字里猜出来的）。
        """
        return RunFacts(
            tools=frozenset(self.registry.names()),
            edits=edits,
            changed=frozenset(changed),
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
        assessment: str = "",
        suggestion: str = "",
        changed: tuple[str, ...] = (),
    ) -> Verdict | None:
        """问一次督导。关着、或者它没能给出结论，都返回 None。
        证据里刻意不含对话历史：要判断的是「这些动作有没有进展」，
        读到主循环的推理只会让它附和那个推理——和审查者必须独立同一个理由。
        它看到的是事实（记录下来的状态、发生过什么、机械统计）。

        **但两样东西必须给它**：每条工具记录的意图（`trace` 里的「意图:」），
        与它上一次建议后被怎么回答的（`assessment` / `suggestion`）。前者回答
        「它按这些按钮是想干什么」，后者回答「我的建议有没有被当成命令」——
        缺了后者，它只能看到「它没照做」，下一个结论就会从建议滑向命令。

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
            assessment=assessment,
            suggestion=suggestion,
        )
        verdict, conflicts = supervise(
            self.gateway,
            evidence,
            max_grant=int(self.config.limit("supervisor_max_grant")),
            facts=self._run_facts(edits=edits, changed=changed),
        )
        if conflicts:
            # 说错话被拦下这件事必须看得见：否则「督导的断言有人核对」这条
            # 规矩坏掉时，日志里什么都看不出来，而它端给模型的是假事实。
            detail = "；".join(f"{one.claim} → {one.truth}" for one in conflicts)
            line = f"督导的断言与记录不符，已按记录改写：{detail}"
            self._log(line)
            self._emit(
                "note",
                {"text": line, "ok": False, "kind": "supervisor_fact_conflict"},
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

        **重复调用的硬拦还要看两件事实**（这一轮用户点名的死锁就在这里）：

        - 上一份结果**还在不在上下文里**：循环在软/硬触发时会砍历史
          （`_context_revision` 在那两处 +1），砍掉之后那个签名虽然还在
          `recent` 窗口里，内容却已经不在了——模型手上什么都没有，
          再拦一次就是「只有一个被拦的 read、别的啥也没有」的死锁。
        - **文件变没变**：它刚改过这个文件（或改动还没落盘、但已经在
          待确认清单里），旧结果描述的是旧文件。

        两者任一不成立 → **照常执行，但把「为什么允许再来一次」写进 trace
        与事件流**（`GUARD_REFRESH_PREFIX`）。放行有上限：`_REFRESH_LIMIT`，
        否则「不断被拦」+ 硬拦退还预算就成了无限预算。
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
            reason = self._refresh_reason(signature, call)
            if reason:
                self._refreshes_used += 1
                trace_line = (
                    f"{GUARD_REFRESH_PREFIX}同一个调用第 {level} 次，"
                    f"但{reason}，因此重新执行一次"
                    f"（已放行 {self._refreshes_used}/{_REFRESH_LIMIT}）"
                )
                self._log(trace_line)
                self._emit(
                    "note",
                    {
                        "text": trace_line,
                        "ok": True,
                        "kind": "guard_refresh",
                    },
                )
                result = self.registry.invoke(call)
                self._note_guard_result(call, result)
                # 这次放行后就地更新「它看过什么」：不更新的话，下一次同签名
                # 进来时第一条判据（版本过期）仍然成立，会连着放行到上限——
                # 那是把刷新当成了常规通道，而它只是一次性的补救。
                self._seen_at_revision[signature] = self._context_revision
                intent = _intent_clause(call)
                return ToolResult(
                    ok=result.ok,
                    content=(
                        f"{GUARD_REFRESH_PREFIX}{reason}，所以这一次重新取了一遍"
                        f"（上面那份已经不在你的上下文里或已经过期）。"
                        f"{self._file_state_note(call)}"
                        + (f"\n{intent}" if intent else "")
                        + f"\n{result.content}"
                    ),
                )
            return ToolResult(
                ok=False,
                content=(
                    f"{BLOCKED_PREFIX}这个调用在最近几步里已经做过 {level - 1} 次，"
                    "结果不会改变，因此没有执行。"
                    "上一步的输出仍在你的上下文里：换一种做法：换一个工具、"
                    "换一组参数，或者先查别的地方；如果上一步的输出已经够了，"
                    "就基于它往下做（提改动 / 给结论），不要再查一遍。"
                    "下一步的意图要写清「这一次想拿到什么、拿到之后做什么」——"
                    "只重复一句「再看一遍文件」不算下一步。"
                    + (f"\n{_intent_clause(call)}" if call.intent else "")
                ),
            )
        result = self.registry.invoke(call)
        self._note_guard_result(call, result)
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

    def _note_boundary_write(self, path: str) -> None:
        """这次写入落在受保护路径上时，当场报一条高严重度发现。

        判据与文案在 `agent/audit.py`（`boundary_write`）——这一层只负责把
        **两个事实**喂给它：改的是哪个路径、这个文件是不是本次运行自己创建的。
        后者问 `PendingChanges`（磁盘上改动之前有没有它），不是猜的。

        可见性走三条路，**都不进模型上下文**：事件流（`note` / `kind=audit`，
        探针与人都数得出来）、trace（督导看的）、收尾报告（`_with_final_state`）。
        不进模型上下文是有意的：这一层是给人看的证据，而每改一个既有文件就往
        对话里塞一段提醒，会挤掉真正要它读的东西。
        """
        if self.pending is None or not path:
            return
        try:
            stored = self.pending.path_of(path)
            created = not self.pending.disk_path(stored).exists()
        except (PathOutsideProjectError, OSError, ValueError):
            return
        finding = boundary_write(
            path=stored,
            created=created,
            config=load_protected(self.config.project_root),
        )
        if finding is None:
            return
        # 同一个文件只报一次：每一次改动都报一遍，等于把这条判据变成噪声。
        if any(
            item.kind == finding.kind and item.summary == finding.summary
            for item in self.audit_log.findings
        ):
            return
        self.audit_log.add(finding)
        self._emit(
            "note",
            {"text": finding.render(), "ok": True, "kind": AUDIT_KIND},
        )
        self._log(f"自动审查：{finding.render()}")

    def _boundary_report(self) -> str:
        """收尾报告里那一段高危发现（没有就返回空串）。

        「**必须可见**」这条要求的落点：发现不能只躺在 `audit_log` 里，
        收尾那一份人真的会读的文本里要有它。
        """
        lines = self.audit_log.high_lines()
        if not lines:
            return ""
        return "\n本次运行的自动审查发现：\n" + "\n".join(f"- {line}" for line in lines)

    def _log(self, line: str) -> None:
        """往本轮的 trace 上追加一行（给人和督导看的那一份）。

        与 `self._emit` 分开：emit 发给外部观察者（网页壳、`--events`），
        trace 是督导的证据。护栏的放行理由两边都要有，所以两个出口各写一次。
        """
        if self._trace is not None:
            self._trace.append(line)

    def _text_of(self, path: str) -> str | None:
        """这个路径现在的内容（**叠加待确认改动之后**的视图）。取不到返回 None。

        必须走 WorkspaceView 而不是读磁盘：`read_file` 给模型的就是这个视图，
        拿磁盘去比会把「文件已经变了」判成没变。
        """
        try:
            view = WorkspaceView(self.config.project_root, self.pending)
            target = resolve_readable(
                self.config.project_root, path, self.read_roots
            )
        except (PathOutsideProjectError, OSError, ValueError):
            return None
        if not view.exists(target):
            return None
        try:
            return view.read_text(target)
        except (OSError, UnicodeDecodeError):
            return None

    def _lingering_text(self, call: ToolCall, result: ToolResult) -> str:
        """这次写入有没有留下「旧名仍在别处」的残留；有就返回一段提醒。

        只在两个前提下才算：这次调用真的改出了一个文件的内容（成功、不是
        暂存），以及待确认清单里确实有这处改动。算不出来一律返回空串——
        它是附加的旁证，不是主流程，不能因为它没算出来而影响一次成功的写入。
        """
        if not result.ok or call.name not in WRITE_TOOLS or self.pending is None:
            return ""
        if result.content.startswith(STAGE_MARKER):
            return ""
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            return ""
        try:
            stored = self.pending.path_of(path)
        except PathOutsideProjectError:
            return ""
        change = next(
            (item for item in self.pending.items() if item.path == stored), None
        )
        if change is None:
            return ""
        try:
            return lingering_note(self.config.project_root, self.pending, change)
        except Exception:
            # 残留报告是给人看的旁证，它自己出问题不该毁掉一次成功的写入。
            return ""

    def _file_changed(self, call: ToolCall) -> bool:
        """这次要读的文件，与上次读它时相比变了没有。"""
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            return False
        expected = self._read_fingerprints.get(path)
        if expected is None:
            return False
        text = self._text_of(path)
        return text is not None and _content_fingerprint(text) != expected

    def _refresh_reason(self, signature: str, call: ToolCall) -> str:
        """重复调用该不该放行。放行返回**理由**，该拦返回空串。

        两个判据都只看事实，不看它看起来是否在打转：

        - 内容已被压掉：装配历史被砍过（`_context_revision` 变了），那个签名
          虽然还在 `recent` 窗口里算「重复」，内容却不在上下文里。硬拦的后果
          是模型手上只剩一份被拦的 read——什么都读不到，也走不出去。
        - 文件变了：它读完之后这个文件被改过（含还在待确认清单里的改动），
          旧结果描述的是旧文件；行号也可能已经漂了。
        """
        if self._refreshes_used >= _REFRESH_LIMIT:
            return ""
        mark = self._seen_at_revision.get(signature)
        if mark is not None and mark < self._context_revision:
            return "上一次的结果已经被上下文压缩丢弃"
        if self._file_changed(call):
            return "这个文件在读取之后已经改过，旧结果描述的是旧内容"
        return ""

    def _file_state_note(self, call: ToolCall) -> str:
        """紧凑的文件状态摘要：行数 + 指纹。

        刻意不重发整份内容（那正是要省的东西）：给的是「现在的状态长什么样」，
        让模型能判断自己的行号还在不在、接下来该重新读还是直接改。
        """
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            return ""
        text = self._text_of(path)
        if text is None:
            return f"{path} 现在不存在或读不到。"
        lines = len(text.splitlines())
        return f"{path} 现在是 {lines} 行，指纹 {_content_fingerprint(text)}。"

    def _note_guard_result(self, call: ToolCall, result: ToolResult) -> None:
        """记下这次读文件的结果指纹，供下一次重复调用比对「文件变没变」。"""
        if not result.ok or call.name != "read_file":
            return
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            return
        text = self._text_of(path)
        if text is not None:
            self._read_fingerprints[path] = _content_fingerprint(text)

    def _forget_read(self, path: str) -> None:
        """这个文件被改过：清掉它的读取指纹，下一次重复读必须能重新取。

        顺带把它的「读于哪个上下文版本」也清掉——留着它，重复调用的第二条
        判据（内容还在不在）会拿到一个描述旧文件的时间戳。
        """
        self._read_fingerprints.pop(path, None)
        for signature in [
            one for one in self._seen_at_revision if f'"{path}"' in one
        ]:
            self._seen_at_revision.pop(signature, None)

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

        `changed` 要传**本次运行累计改过的全部文件**（不是某一轮的半成品）：
        验证器据此决定在哪个范围里验，而多文件改名/搬移天生多步——
        对半新半旧的树逐步验证必然失败，且必然诱发「为了让测试通过而改坏
        业务逻辑」（rename-cross-file 复跑实测，见 spoolkit.agent.audit）。

        跑完之后过一遍自动审查层 L1：回退、中间态、迎合测试三类发现
        由 `spoolkit.agent.audit` 机械地判出来，在这里挂上事件与账本。

        **返回的每一句都会进提示**，所以出口统一过 `for_prompt`：验证正文里
        带着本次试跑副本的随机目录名（`…\\spool-trial-<8 位随机>\\project`，
        来自 pytest 的 `PytestCacheWarning`）时，两条同设置的运行会因此在
        同一个位置产生不同的 token 数，贪心解码随即放大成两条轨迹——实测
        20 行事件流里就差 `usage` 与 `final` 两行。路径本身不丢：给人看的那
        一份在 `ToolResult.raw` 与事件流里原样保留。
        """
        try:
            result = self.verify(changed or [])
        except Exception as exc:
            # 异常正文也常带临时路径（"没有这个文件: …\\Temp\\spool-trial-x\\…"）。
            return (
                for_prompt(f"自动验证无法运行，错误类型 {type(exc).__name__}：{exc}"),
                False,
            )
        body = " ".join(for_prompt(result.content).split())[:400]
        if result.content.startswith(ENVIRONMENT_MARKER):
            # 记下来：任务没做成时，这决定要不要替它登记诊断请求。
            self.environment_blocked = True
        # 结论、覆盖过的路径、以及**这些路径当时的内容指纹**三样一起留档：
        # 收尾报最终状态时，只有内容一字未变才允许沿用这一次的结论。
        self._last_verify = (body, result.ok)
        self._last_verify_key = self._content_key(changed) or ""
        # ---- 自动审查层 L1：这次验证的结论有什么可疑 ---------------------------
        # 三类都只看**机械事实**：报错里的旧名、测试文件是不是被还原了、
        # 业务代码的行数差了多少。高危**不阻断**（本轮只做检测 + 报告 + 挂点）。
        self._last_findings = ()
        fallback = ""
        if SCOPE_FALLBACK_MARKER in result.content:
            fallback = result.content.split(SCOPE_FALLBACK_MARKER, 1)[1]
            fallback = fallback.splitlines()[0].strip()
        found = audit(
            ok=result.ok,
            ran_scope=".",
            changed_paths=list(changed or []),
            failed_test_paths=self._failing_test_paths(result.content),
            reverted_paths=[
                change.path
                for change in (self.pending.items() if self.pending is not None else [])
                if is_test_path(change.path)
            ],
            renamed=pending_renames(self.pending),
            shrunk=self._shrunk_business_files(changed or []),
            detail=result.content,
            # 原始报错：短摘要里已经没有了 `cannot import name 'get_rows'`
            # 那一行，而判「这次失败是不是改名没铺完」正需要它。
            raw_detail=result.raw or result.content,
            fallback_reason=fallback,
        )
        # 中间态这一条不需要「失败」之外的门槛，但它只在报错里出现本次被改掉的
        # 旧名时才成立——那正是「改名只落了一半」的形状（见 audit 模块头）。
        if result.ok:
            found = Audit(
                tuple(
                    item for item in found.findings if item.kind == "scope_fallback"
                )
            )
        self.audit_log.extend(found)
        self._last_findings = found.findings
        if found.findings:
            # 发现也要进 trace：调试时「这一步判出了什么」是唯一能看到的信号，
            # 而高危那条（迎合测试）本来就该在人读的轨迹里留一行。
            self.audit_trace(found)
        if result.ok:
            # 成功那句**不能请它收尾**。验证只覆盖「这次改动的地方」，
            # 任务整体做完没有它并不知道；而一句「没有别的要改就收尾」，
            # 实测在 20 道题的铺盘里做完 5 道就让它宣布完成——
            # 它把「这一批做完」当成了「任务完成」。
            return (
                for_prompt(
                    f"系统自动跑了一遍测试：通过（{body}）。"
                    "这说的是这次改的地方通过了。"
                    "任务整体做完没有由你判断：还有未完成的接着做，"
                    "确实都做完再给出结论。"
                ),
                True,
            )
        # 失败到底是什么性质，由验证器说——它才知道退出码的含义。
        # 循环这边再加一句「测试失败」会盖掉那个区别。审查发现跟在后面：
        # 它回答的是「这次失败意味着什么」，那正是模型最容易判错的一步。
        verdict = found.render()
        if verdict:
            return (for_prompt(f"自动验证的结果：{body}\n{verdict}"), False)
        return (for_prompt(f"自动验证的结果：{body}"), False)

    def audit_trace(self, found: Audit) -> None:
        """把发现写进本步的 trace。

        单独成一个方法是为了不改 `_run_verification` 的签名（它被直接调的
        用例钉着）。`self._trace` 在 run() 之外可能是 None，那就只更新账本。
        """
        if self._trace is None:
            return
        for finding in found.findings:
            self._trace.append(f"自动审查：{finding.render()}")

    @staticmethod
    def _failing_test_paths(content: str) -> list[str]:
        """从验证结论里挑出被点名的测试文件。

        只认 `FAILED` / `ERROR` 后面那个路径：那是 pytest 自己报的，
        不是猜的。挑不出来就返回空表——中间态那条判据会因此不成立，
        宁可不报也不乱报。
        """
        found: list[str] = []
        for raw in content.replace("｜", "\n").splitlines():
            line = raw.strip()
            for prefix in ("FAILED ", "ERROR "):
                if not line.startswith(prefix):
                    continue
                path = line[len(prefix):].split(" ", 1)[0].split("::", 1)[0]
                if path and path not in found:
                    found.append(path)
        return found

    def _shrunk_business_files(
        self, changed: Sequence[str]
    ) -> list[tuple[str, int, int]]:
        """改动里的**业务**文件（非测试）比磁盘原始版本更短的那些。

        行数是可证伪的：`_base_lines` 记的是这个文件第一次被改之前磁盘上的
        行数，「现在多少行」从待确认改动的新正文里数。测试文件不算——
        那是另一类问题（验证层已按原始内容判定并写在结论里）。
        """
        shrunk: list[tuple[str, int, int]] = []
        if self.pending is None:
            return shrunk
        for change in self.pending.items():
            if change.path not in changed or is_test_path(change.path):
                continue
            if Path(change.path).is_absolute():
                continue
            before = self._base_lines.get(change.path)
            if before is None:
                continue
            after = len(change.new_text.splitlines())
            if after < before:
                shrunk.append((change.path, before, after))
        return shrunk

    def _disk_lines(self, path: str) -> int:
        """工作区里那个文件现在的行数（读不到就 0，判据随之不成立）。"""
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self.config.project_root / path
            return len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return 0

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

