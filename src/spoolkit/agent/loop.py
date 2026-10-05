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
    ACTION_BLOCK_KIND,
    AUDIT_KIND,
    Audit,
    AuditLog,
    BLOCKED_REVIEW_HARD_KIND,
    BLOCKED_REVIEW_KIND,
    CANNOT,
    ESCALATE_KIND,
    EVIDENCE_GAPS_KIND,
    FAIL,
    Finding,
    HIGH,
    INFO,
    NOT_RUN,
    PASS,
    ReviewLedger,
    SEMANTIC_FINDING_KIND,
    TESTS_WEAKENED_KIND,
    audit,
    boundary_write,
    load_protected,
    note_kind,
    tests_weakened,
)
from spoolkit.agent.monitor import ToolSight, inspect
from spoolkit.agent.protocol import ParseFailure, build_turn_schema, parse_turn
from spoolkit.agent.review_behavior import (
    BEHAVIOR_CHANGED_KIND,
    BEHAVIOR_NOT_CHECKED_KIND,
    BEHAVIOR_READINGS_KIND,
    CLI_SURFACE_CHANGED_KIND,
    PERFORMANCE_REGRESSED_KIND,
    BehaviorReport,
    cli_help_candidates,
    compare_cli_help,
    review as review_behavior,
)
from spoolkit.agent.review_gate import (
    PENDING as ESCALATION_PENDING,
    LINKED,
    OUTSIDE,
    EscalationDecision,
    EscalationGate,
    EscalationRequest,
    outside_write_refusal,
    parse_reply,
    render_decisions,
    render_summary,
)
from spoolkit.agent.review_semantic import (
    L3_BLOCKED_KIND,
    L3_DETAIL_KIND,
    L3_READINGS_KIND,
    ChangeFacts,
    ReviewMaterials,
    SemanticReport,
    TriggerDecision,
    as_findings as semantic_findings,
    blocked_payload as semantic_blocked_payload,
    decide_triggers as semantic_triggers,
    reading_payload as semantic_reading_payload,
    render_report as render_semantic_report,
    render_unverified as render_semantic_unverified,
    review as review_semantic,
)
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
    BROKEN_IMPORT_KIND,
    CONFIRM_TOOL,
    DELETED_REFERENCE_KIND,
    DELETE_TOOL,
    NEXT_STEP_MARKER,
    REFUSAL_MARKER,
    STAGE_MARKER,
    STALE_REFERENCE_KIND,
    STALE_REFERENCES_KIND,
    broken_local_imports,
    is_test_path,
    lingering_hits,
    lingering_note,
)
from spoolkit.tools.capabilities import (
    COMMIT_TOOLS,
    EDIT_TOOLS,
    PROGRESS_TOOLS,
    WRITE_TOOLS,
)
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult
from spoolkit.tools.verify import (
    CANNOT_CHECK,
    ENVIRONMENT_MARKER,
    NO_CHECKER,
    SCOPE_FALLBACK_MARKER,
    pending_renames,
)
from spoolkit.tools.view import WorkspaceView

SYSTEM_PROMPT = T.SYSTEM

LOOKUP_TOOLS = ("find_symbol", "file_symbols", "find_callers")

# 写工具那几档清单（EDIT_TOOLS / WRITE_TOOLS / COMMIT_TOOLS / PROGRESS_TOOLS）
# **不在这一层手写**：它们由 `spoolkit.tools.capabilities` 从**每件工具的能力
# 元数据**派生（`writes` / `destructive` / `commits` / `progress`），这里只做转出。
#
# 为什么不各留一份：这几档回答的是「哪几把工具能把工作区变成我要的样子」，
# 那是**工具的能力**，不是这一层的判断。两份清单各自演化必然漂移，而且漏一个
# 名字不会有任何报错——`edited` / `changed_paths` / 自动验证的范围、收窄后的
# 采样空间都会静默少一块。原先写在这里的两条事实（删除必须与写工具并列；
# 确认入口必须留在收窄后的采样空间里）现在逐条写在 `capabilities.py` 的声明
# 与派生规则旁边，并由 `tests/tools/test_capabilities.py` 的冻结快照钉住。

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

# 硬拦（没有执行）时**退还步数预算**的判据。
#
# 口径只有一条，与工具名无关：**这一步里的每一个调用都没有真正执行**
# （全部被护栏拦下）→ 这一步没有产出、也没有换来任何新信息 → 不占步数预算。
#
# 这里原先是一张工具名白名单（编辑类 + 几个查看类）。白名单的毛病是漏写一个
# 名字不会有任何报错，症状是隐性的：实测 `find_symbol` 不在名单里，它带着未知
# 参数被硬拦之后照旧吃掉 1 步（探针 l5-set3 的 `rename-cross-file-r0`，第 1 步
# 被拦、trace 里连退还那一行都没有，8 步预算少一步）。判断依据从「工具是不是
# 查看类」换成「这一次有没有真的执行」，名单就不用维护了，也不会再出现
# 「同一件事在两个地方两个口径」。
_BLOCKED_STEP_KIND = "blocked_refund"

# 「同一个（工具 + 参数）被拦到阈值 → 该组合停用」这件事的**事件种类**。
#
# 停用必须看得见：报告与事件流里要能回答「这一次为什么突然换说法了」，
# 否则调这块时只能从散文里猜（`_refuse` 里发，探针按 kind 计数）。
COMBO_DISABLED_KIND = "combo_disabled"

# 「这一步没有验证器可跑」的**事件种类**（问题⑤）。
#
# 它不是发现、也不是失败：验证器如实说「这次改的是 .txt，没有对应的自动验证器」，
# 那是一个读数。单独成一类是为了让它**不再冒充**「自动验证 → 成功」——探针要能
# 机械地分开「验过并通过」与「压根没验」，靠读散文分不开。
#
# 它现在挂在**收尾那条 `最终验证` 阶段行**上（`kind` 与 `no_checker` 两个字段一起
# 带出去），步骤区不再另发一条 note：那一档在用户可见面上只该出现一次，而收尾
# 那条阶段行天然排在最晚（见 `_emit_final_verify`）。
NO_CHECKER_KIND = "verifier_no_checker"


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

#: 「放行一次有理由的重取」这条事件在事件流里的 kind。
#:
#: 它必须与「护栏只提醒、没拦、那一次真的又跑了一遍」分开：后者是**空转**
#: （探针的 `repeat_blocks_unblocked` 判的就是它），前者是护栏**判过**之后
#: 有意放行的。两者都让调用真的执行了一次，光看 `ok` 与工具名分不出来。
GUARD_REFRESH_KIND = "guard_refresh"

#: 「这一次判不出该不该拦」的事件 kind。判据见 `_guard_repeat_decision`。
#:
#: 与 `guard_refresh` 分开是因为它回答的是另一个问题：`guard_refresh` 说
#: 「我知道为什么允许」，这一条说「我**没有**判出来」——不许把没判成判过
#: （那会让「空转归零」变成一个说法，而不是一个读数）。放行行为一样，
#: 理由字段不一样。
GUARD_UNDECIDED_KIND = "guard_repeat_undecided"

#: 「重复调用但**已经判明有理由**」时进工具输出的前缀。
#:
#: 与 `GUARD_REFRESH_PREFIX` 同一个位置（都进模型可见文本），单独一个前缀是
#: 因为它承载的是一句**判据结论**：内容指纹变了。理由也写在同一行里。
REPEAT_REFRESH_PREFIX = "⟳ 重复调用放行："

# ---- 重复调用的三种归类（`_guard_repeat_decision` 的返回值）------------------
#
# 这一组是本轮的核心：**第一次重复**不再一律「只提醒」，也不再一律硬拦，
# 而是先用机械判据把两种重复分开。三种归类各有各的处置，一个都不许省：
#
#   `REPEAT_USELESS`    参数完全相同 + 该对象自上次调用以来**没有可观察变化**
#                       + 上一次结果**还在上下文里** → **第一次就硬拦**，
#                       并走既有的换路那一套（`_refuse` → `combo_disabled` →
#                       替代清单）。它不占步数预算、不计入「未被拦的重复」。
#   `REPEAT_REFRESH`    内容指纹变了，或上下文版本推进导致原结果已被压掉
#                       → **放行**，并把「为什么允许」写进 trace 与事件流
#                       （`kind=guard_refresh`）。它不是空转。
#   `REPEAT_UNDECIDED`  拿不到指纹、也没有可观察变化可比较（`run_command`
#                       这类没有路径的调用）→ 保留「提醒」路径，并**记录
#                       「这次判不出」**（`kind=guard_repeat_undecided`）。
#                       不许假装判过：假装判过等于把「空转归零」变成一个说法。
REPEAT_USELESS = "useless"
REPEAT_REFRESH = "refresh"
REPEAT_UNDECIDED = "undecided"


def _content_fingerprint(text: str) -> str:
    """一段内容的指纹：sha1 前 12 位 + 行数。

    行数不参与哈希，是要让它**同时**回答两个问题：内容变没变、行号漂没漂。
    只给哈希的话，「文件变了 3 行」这件事在读回里看不见，而它正是
    replace_lines 出错的原因。
    """
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{digest}/{len(text.splitlines())}行"


def _flat_line(text: str) -> str:
    """压成一行。收尾报告里「失败要点」只有一行的位置，换行会把后面挤没。"""
    return " ".join((text or "").split())


#: **只给模型看**的那一句（第二人称指令）。它进模型上下文，绝不进任何用户可见面。
#:
#: 这一轮修的残渣之一就是它漏过一次：`_run_verification` 的返回值既是"回灌给
#: 模型的反馈"，又被原样当成事件正文发出去（步骤区那条 note、`自动验证` 与
#: `最终验证` 的 detail），用户于是在助手卡片上读到系统对他说的话。
#: 修法不是逐个字面量去删，而是把两侧**分开**：这句只挂在模型那一侧，
#: 所有 `_emit` 一律用验证器的原话（`self._last_verify[0]`）。
_MODEL_ONLY_TAIL = (
    "任务整体做完没有由你判断：还有未完成的接着做，确实都做完再给出结论。"
)

#: 收尾报告里「详情」那一行的行首标记（用户可见，但不进结论段）。
#:
#: 网页壳据此把详情行折进「思考过程」（`web/runner.py::split_report`）：
#: 结论段只留「做成了什么 + 验没验 + 我能做什么」，落成计数 / 文件状态 / 指纹
#: 这些账目属于详情。它是**报告正文的一部分**（终端那条路照样打出来），
#: 所以标记本身也要读得通。
DETAIL_MARK = "详情："


#: 「这一轮压根没有验证器」的识别字面量（问题⑤）。
#:
#: 两处都要认：`tools/verify.py::lazy_file_verify`（按扩展名分派那条路，原话
#: 「这次改的是 .txt，没有对应的自动验证器。」）与 `NO_CHECKER` 标记
#: （`verify_file` 那条路）。**它不是失败**（`ok=True`），但它也**不是
#: 「验证通过」**——两句话同时出现在一份报告里就是自相矛盾。
_NO_CHECKER_MARKS = ("没有对应的自动验证器", NO_CHECKER)


def _says_no_checker(text: str) -> bool:
    """这段验证正文是不是「这次没有验证器」那一声。"""
    body = str(text or "")
    return any(mark in body for mark in _NO_CHECKER_MARKS)


def _no_checker_note(body: str) -> str:
    """**回灌给模型**的那一句：「没有校验器（不是失败）」+ 验证器给的具体内容。

    验证器的原话里带着「没有对应的自动验证器」这半句，而它在同一份文本里与
    「自动验证 → 成功」摆在一起就是自相矛盾（问题⑤）。所以这一档统一说
    「没有校验器（不是失败）」，后面接上验证器给的具体内容（改的是哪一类文件）。

    **它只进模型上下文与 trace**：用户可见面上的结论段用 `NO_CHECKER_LINE`
    （不带"改的是哪一类文件"），过程区那一档状态字用 `VERIFY_SKIPPED`。
    """
    text = _flat_line(str(body or "").replace("没有对应的自动验证器", ""))
    # 去掉替下来之后剩下的那个标点：原话是「这次改的是 .txt，没有对应的自动
    # 验证器。」，直接删短语会留下「这次改的是 .txt，。」这种半句话。
    text = text.strip().strip("。").rstrip("，,、；;：:").strip()
    return "没有校验器（不是失败）" + (f"：{text}。" if text else "。")


#: 第三档（没有校验器）在**用户可见面**上的两个说法，各有各的分工：
#:
#: - `NO_CHECKER_LINE`：**结论段**那一句。它回答"这次验没验"。
#: - `VERIFY_SKIPPED`：**过程区**那一档状态字（阶段行与 trace）。它回答
#:   "这一步真的跑了吗"。跑不了就说「跳过」，绝不冒充「成功」。
#:
#: 两句**刻意不同**：同一句在用户可见面上出现两次，正是这一轮修掉的残渣之一
#: （现场见 `tests/fixtures/interact_probe/web-real-reports.md`：第 1 步那条 note
#: 与结论段说的是同一句「没有校验器（不是失败）」）。
NO_CHECKER_LINE = "没有校验器（不是失败）。"
VERIFY_SKIPPED = "跳过（不是失败）"


def _no_checker_reason(body: str) -> str:
    """「没有校验器」的**理由**那半句（例：`这次改的是 .txt。`），不带那一档说法。

    `最终验证` 那条阶段行的 detail 用它：状态字由 `VERIFY_SKIPPED` 给、
    理由由验证器原话给——两处各说各的，不把同一句说两遍。
    """
    _head, sep, reason = _no_checker_note(body).partition("：")
    return reason if sep else ""


def _verify_event_detail(body: str, limit: int = 200) -> str:
    """事件流里那条验证细节。

    没有校验器时给的是**理由**（`_no_checker_reason`），不是那一档说法：
    状态字由渲染档位给（`web/runner.py` 的第三档 = `VERIFY_SKIPPED`），
    正文里再说一遍「没有校验器（不是失败）」就是同一句话出现两次。

    其余情况给验证器的**原话**。这一条原先拿的是"回灌给模型的那段反馈"，
    于是系统对模型说的话（第二人称指令）漏到了用户可见面上——所以调用方给的
    必须是 `self._last_verify[0]`（验证器原话），不是 `_run_verification` 的返回值。
    """
    text = _no_checker_reason(body) if _says_no_checker(body) else str(body or "")
    return " ".join(text.split())[:limit]


#: 「行为面」：改到这些类型才谈得上行为回归（L2）。一次只写了个 `.txt` 的运行
#: 不该为「行为回归查不了」铺一整段（问题⑥）。
_BEHAVIOR_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".html", ".htm", ".css", ".scss", ".sass", ".vue", ".svelte",
        ".java", ".kt", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp",
        ".rb", ".php", ".cs", ".swift", ".sh", ".ps1", ".bat", ".cmd", ".sql",
    }
)


def _has_behavior_surface(changed: Sequence[str]) -> bool:
    """这一批改动里有没有「会改变行为」的产出类型。"""
    return any(Path(str(item)).suffix.lower() in _BEHAVIOR_SUFFIXES for item in changed)


def _verify_brief(body: str, limit: int = 60) -> str:
    """③ 验证那一句的要点：一行、有上限。

    验证正文常常是「退出码 0 $ python -m pytest -q …[100%] … 警告若干」——
    整段搬进报告就把人读的那一份变成了日志。要点留在这里，全文进事件流的
    `最终验证` 那条细节与 trace。
    """
    text = _flat_line(body)
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit].rstrip("，,。;； ") + "…"
    return text


def _undo_action_line(command: str) -> str:
    """⑤ 可做的动作：**一次**，写成动作而不是「请复制这条命令」。

    命令本身照旧逐字给出来（既有验收里有用例把它抄下来真执行），但那句话
    的主语从「你去复制」变成「这里有一个动作」：网页壳把它做成可点的按钮
    （`POST /revert`），桥里回一个 `y` 就走同一条路。
    """
    return f"撤销这一轮：{command} （网页里可直接点，桥里回 y）"


def _is_blocked(result: ToolResult) -> bool:
    """这次失败是「被拦下没执行」，而不是「执行了但失败」。"""
    return (not result.ok) and result.content.startswith(_BLOCKED_MARKS)


#: 机械发现的判据名 → 人读的一句话。判据本身在 `tools/edit.py`，
#: 这里只负责把它翻成收尾报告里那一行（**每条都带 `文件:行`**）。
_HIT_SUMMARY = {
    STALE_REFERENCE_KIND: "改名后旧名仍有残留",
    DELETED_REFERENCE_KIND: "删掉的文件/符号仍被引用",
    BROKEN_IMPORT_KIND: "导入的本地模块不存在",
}

#: `_mechanical_review` 那几条判据产出的**发现类别**：收尾重判时要整批作废、
#: 按终态内容重新登记（旧判词描述的是中途那一版内容，见 `_rejudge_mechanical`）。
#: 名单必须与 `_mechanical_findings` 实际产出的 kind 一致，否则会漏撤或误撤。
MECHANICAL_KINDS = frozenset(
    {
        STALE_REFERENCE_KIND,
        DELETED_REFERENCE_KIND,
        BROKEN_IMPORT_KIND,
        TESTS_WEAKENED_KIND,
    }
)

#: L2（P2 行为回归）那几条判据产出的**发现类别**。收尾重判时要按同一份终态
#: 内容重算，所以它们和 L1 一样整批作废（见 `_rejudge_mechanical`）。
BEHAVIOR_KINDS = frozenset(
    {
        BEHAVIOR_CHANGED_KIND,
        CLI_SURFACE_CHANGED_KIND,
        PERFORMANCE_REGRESSED_KIND,
        BEHAVIOR_NOT_CHECKED_KIND,
    }
)

#: L3（P2 后半，语义审查）产出的**发现类别**。它单独一份的理由与上面两组
#: 不同：L1/L2 是每轮都能重算的机械判据，而 L3 是**一次模型调用**——它的
#: 判词同样只对某一版内容成立（缓存键含内容指纹），但**不能**在每次收尾时
#: 无条件重跑（那就成了「每步都跑」）。所以它的作废与重算是同一个入口
#: （`_semantic_review`：键变了才重跑），这里只登记类别名。
L3_KINDS = frozenset({SEMANTIC_FINDING_KIND})


def _hit_finding(hit) -> Finding:
    """把一条机械命中翻成发现：`文件:行` 落在 `detail`，判据名落在 `kind`。

    高严重度**不阻断**（P0 的纪律）：严重度只用于「收尾报告里顶一行」，
    阻断语义是 P1 的事。
    """
    where = hit.where()
    detail = f"{hit.name} @ {where}——{hit.detail}" if hit.detail else f"{hit.name} @ {where}"
    return Finding(
        kind=hit.kind,
        severity=HIGH,
        summary=_HIT_SUMMARY.get(hit.kind, hit.kind),
        detail=detail,
        escalate=True,
        path=hit.path,
        line=hit.line,
    )


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


def _render_args(call: ToolCall) -> str:
    """把这次重发的参数**逐字**写出来。

    为什么必须有这一行：实测（探针 l5-set3 的 `rename-cross-file-r1`）模型把
    同一条 `search_code` 连着原样发了三次，而它三轮写的意图是「去掉多余参数」
    「严格按督导建议的形状发」——它**以为自己改了**。护栏原来的话术只解释了
    「为什么不能重发」，从没把「你这次发的到底是什么」摆到它面前，于是它手上
    有一个假事实：它改的是脑子里的形状，发出来的是同一串字节。
    """
    try:
        return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - 模型给不出这类值
        return repr(call.arguments)


def _reverse_clause() -> str:
    """§2.5 的反向条款：把「拦的是这一次动作」与「不许删文件」切开。

    用户点名过两次：这段话术被读成「一律不许删 / 不许精简」时，模型会把正常的
    合并、删死代码当成违规动作而不敢做，任务反而做不完。所以每条拦截话术都要
    带上这一句，而且是**同一句**（两处说法不一等于两套语义）。
    """
    return (
        "拦的是**这一次动作**，不是「不许删文件 / 不许精简代码」：删死代码、"
        "合并重复实现、删已被替代的文件都是正常编辑，改动照落盘、照样可回退"
        "（§2.5 反向条款）。"
    )


def _remedies(failure: str) -> list[str]:
    """从失败话术里抽出「照做的下一步」那几行（能照抄的调用形状）。"""
    return list(
        dict.fromkeys(
            line.strip()
            for line in (failure or "").splitlines()
            if line.strip().startswith(NEXT_STEP_MARKER)
        )
    )


def _retry_block_message(call: ToolCall, failure: str) -> str:
    """原样重提一个失败过的调用时的话术：**给出能照做的下一步**。

    只说「换一种做法」等于把问题还给模型——dogfood 里它就是这么原样重试
    到预算耗尽的。失败话术里已经带了「照做的下一步」时原样端回来；
    没带时给三条具体可选的替代（换参数、换工具、先读原文）。

    暂存那一种要分开说：它**不是失败**（正文收下了），但把同一份正文再发
    一遍是这次运行里最贵的一种浪费——输出预算正是卡在这里的。
    """
    remedy = _remedies(failure)
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
            f"你这次重发的参数（与上一次逐字相同，问题就在这串参数里）："
            f"{_render_args(call)}",
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
        parts.extend(remedy)
    elif not staged:
        parts.append(_generic_remedies())
    parts.append(_reverse_clause())
    return "\n".join(parts)


def _generic_remedies() -> str:
    """给不出具体形状时的替代动作清单（四条，都能照着做）。"""
    return (
        "换一种做法，四选一：① 换参数（换行号区间、先多带几行上下文）；"
        "② 换工具（read_file 先看原文 / write_file / replace_lines / "
        "replace_text）；③ 先看别的（读另一个文件、换一个检索词）；"
        "④ 直接给结论（任务确实已经做完时）。"
    )


def _combo_disabled_message(
    call: ToolCall, count: int, remedies: Sequence[str], original: str
) -> str:
    """同一个（工具 + 参数）组合被拦到阈值之后的话术：**停用这个组合**。

    只递一句「为什么不能重发」拦不住它：实测同一条调用被拦了三次，模型每一轮
    的意图都写着「按要求改好了」，发出来的参数一字未改。所以这里换个做法——
    **不再讨论这次调用**，而是宣告这个组合停用，并把它能走的路列成清单；
    下一个动作必须从清单里选一条（`loop.py` 主循环还会把这条轨迹交给督导，
    附上「已被拦几次 + 已经给过哪些替代方案」，不让它自己空转到上限）。

    这条话术与常规那条必须**不是同一句话**：模型对同一句话是免疫的
    （实测重复提醒、拦截说明都照发不误），换的必须是它读到的东西。
    """
    alts = list(remedies) or [_generic_remedies()]
    lines = [
        f"{BLOCKED_PREFIX}（工具 {call.name} + 同一组参数）这个组合已经**停用**："
        f"它已经被硬拦 {count} 次，每一次的参数都**完全相同**，"
        "再发一次不会有别的结果，所以从这里起直接拒。",
        f"停用的组合参数：{_render_args(call)}",
        "替代动作清单（下一条必须从这里选，选哪条都行）：",
    ]
    lines.extend(f"  · {one}" for one in alts)
    lines.append(
        "  · 这条组合里的东西你已经拿到过：直接用已有的输出往下做"
        "（提改动 / 给结论）。"
    )
    lines.append(_reverse_clause())
    if original:
        lines.append(f"上一次的原话（备查）：{' '.join(original.split())[:220]}")
    return "\n".join(lines)


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

# 只有这几类工具的结果算「进度」（清单由 `capabilities` 的 `progress` 那一档派生，
# 与上面那几档写工具清单同一个来源）。读文件和搜索不算：它们是手段，
# 不是产出，记进去只会把状态撑满噪声。
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
    # 收尾那一刻对最终文件的验证结论（None = 没跑或没改动）。
    # 收尾报告之后的收尾处理（settle）要拿它把「最终验证：未通过 + 撤销命令」
    # 摆到 diff 之前——那句话不能只藏在报告正文里。
    verified: bool | None = None
    verify_summary: str = ""

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
        escalation_review: Callable[[EscalationRequest, str], tuple[str, str]] | None = None,
        gate_network: bool = True,
        defer_final_verify: list | None = None,
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
        # 「最终验证」那一条阶段行的**推迟出口**（见 `_emit_final_verify`）。
        # 不给就是当场发（终端那条路：`settle` 与收尾在同一口气里，没有"等确认"
        # 这一段）；`--events` 那条路给一个列表，由调用方在结算之后发出去。
        self._deferred_final_verify = defer_final_verify
        # ---- 风险分级放行（P1，§3.2）----------------------------------------
        #
        # `escalation_review(request, text) -> (批复词, 通道)` 是**注入的批复通道**：
        # 终端/网页壳/聊天通道各自注入自己的那一份，测试注入一份脚本化的回答。
        # **不给就是无人值守**：不弹问题、把动作挂起为待批、成果照落盘（§3.2.3）。
        # 这一层刻意不直接读 stdin：`--events` 模式下读 stdin 会把网页壳/桥的
        # 协议搞乱（它们已经有一条 `--ask-on-stdin` 的通道，别在这里再开一条）。
        self.escalation_review = escalation_review
        self.escalation_gate = EscalationGate(
            config.project_root,
            limit=config.limit,
            run_id=getattr(pending, "run_id", "") or "",
            # 挂起清单只放在**会话目录**里：跨结算要保留的只有「还没执行的动作」，
            # 成果已经在工作区里了（§2.5）。
            pending_path=config.state_dir / "review" / "pending-actions.json",
            # 关联目录：T6 的分流要看它（关联目录里的写拦下来请示一次，
            # 域外直接拒绝）。不传的话「已授权」这件事在这一层看不见，
            # 落在关联目录里的写会被误判成域外、被硬拒。
            extra_roots=self.read_roots,
            # 出网的**动作入口**在主循环（`research`）：子会话里关掉，否则
            # 研究会话连一次 web_fetch 都执行不了（见 EscalationGate 那段注释）。
            gate_network=gate_network,
        )
        # 自动修有限轮：命中「不许宣称完成」时先回灌发现让它自己修，轮数用完
        # 才按 §3.1 报「未完成 + 逐条 file:line」。**默认 0（关）**：自动修要
        # 再花一轮模型调用，短预算的运行（探针 8 步、用例 2–4 步）付不起，
        # 而它是可覆盖的标定值（`.agent/limits.json` 的 `review_fix_rounds`）。
        self._review_fix_limit = int(config.limit("review_fix_rounds"))
        self._review_fix_rounds = 0
        self._review_blocked = False
        self._review_block_note = ""
        # 这一次被拦下的东西**有没有可修的**（高严重度发现才有）。纯规模条件
        # 再问一次也修不掉，所以不浪费那一轮自动修。
        self._review_fixable = False
        # 「不许宣称完成」这条 note 本次运行发过没有。**一次运行只发一条**：
        # 自动修几轮就发几条的话，`blocked_by_review` 会等于轮数，而它与
        # `check_ok` 一起派生的 `blocked_but_ok`（「被阻断但验收绿」）就变成
        # 误报源——那个字段量的是**运行**，不是「判了几次」（§6 字段表）。
        self._review_note_sent = False
        # 失败过的调用（指纹 → 次数 / 上次的失败话术）。用来硬拦原样重试，
        # 并把它失败时看过的「照做的下一步」端回去。每次 run() 开始时清空。
        self._failed_calls: dict[str, int] = {}
        self._failed_output: dict[str, str] = {}
        # 同一个（工具 + 参数）组合被**硬拦**了几次（指纹 → 次数）。到
        # `reroute_block_at` 次之后这个组合停用，下一个动作必须换成别的路。
        # 与失败表同生共死（有改动真的落成时一起清零）：文件变了，同一组参数
        # 可能重新有意义——否则「停用」会变成一个永远不会解除的禁令。
        self._blocked_combos: dict[str, int] = {}
        # 停用发生时留给督导的证据（"已被拦几次 + 已经给过哪些替代方案"）。
        # 主循环下一轮**先叫督导**再让模型说话：模型对同一句话是免疫的
        # （实测同参被拦 3 次），这件事该给判断，不该让它空转到预算上限。
        self._reroute_evidence = ""
        # ---- 重复调用护栏的三条判据（见 _guard_repeat_decision）----------------
        # ① 上一次结果还在不在上下文里：装配时的历史版本号。
        self._context_revision = 0
        self._seen_at_revision: dict[str, int] = {}
        # ② ③ **这中间有没有可观察的变化**：每个签名上一次真的执行时的两条指纹
        # ——调用对象的状态 + 待确认清单。缺这个键 = 它从没执行过。
        #
        # 用指纹而不是「改动次数」：次数分不出「同一个调用的两次之间什么都没
        # 发生」（暂存本身就会把它推上去），而指纹直接回答「对象变了没有」。
        self._observed_state: dict[str, tuple[str, str]] = {}
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
        # 收尾那一刻报出去的那次验证结论（`_with_final_state` 写）：
        # 收尾处理要拿它把「最终验证：未通过 + 撤销命令」摆到 diff 之前。
        self._final_verify: tuple[bool, str] | None = None
        # 自动审查层 L1 的账本（见 spoolkit.agent.audit）。每次 run() 开始时清空：
        # 它是「本次运行」的发现，不是跨任务的记忆。
        self.audit_log = AuditLog()
        # 最近一次验证**新加**的那几条发现。事件流按它发 note，
        # 而不是把整本账每步重发一遍（重发等于给上下文和日志灌噪声）。
        self._last_findings: tuple = ()
        # 机械发现（残留 / 引用闭合 / 测试改弱）已经发过哪些：同一条在每个验证点
        # 会被重新算出来，不去重就会把日志灌满（设计稿 §4 的噪声控制）。
        self._reviewed_notes: set[tuple] = set()
        # ---- L2 行为回归（P2，§5 L2）------------------------------------------
        #
        # 对照运行要起子进程、还要复制两棵「改动前/后」的树，所以**一次运行
        # 只跑一遍**：收尾那条捷径与收尾本体都会走到这里，重跑只是更慢、
        # 读数一模一样（缓存键是那一批改动的路径+内容指纹，内容变了就重跑）。
        self._behavior_report: BehaviorReport | None = None
        self._behavior_key: str = ""
        # ---- L3 语义审查（P2 后半，§5 L3）-------------------------------------
        #
        # 它是一次**模型调用**（成本 ≈ 主循环一步），所以三条纪律一起落地：
        # ① 只在触发条件满足时跑（`semantic_triggers`）；② 一次运行里同一份内容
        # 只跑一遍（缓存键 = 那一批改动的路径 + 内容指纹，与 L2 同口径）；
        # ③ 它坏了、没给结论、或结论解析不出来时**不据此阻断**——那三种情况
        # 都是「这次审查没给出东西」，把它们当成缺陷会让每次失败调用都去拦
        # 一次完成声明（§4 的误报代价）。
        self._semantic_report: SemanticReport | None = None
        self._semantic_key: str = ""
        #: 触发条件判过一次没有。它的理由是**读数**，一份就够；每一版内容各发
        #: 一条只会把事件流灌满（同一条信息不出现两遍）。
        self._semantic_decision_sent: bool = False
        #: 「L3 拦下完成声明」这条 note 发过没有（一次运行只发一条，与
        #: `_review_note_sent` 同一个理由：探针 `l3_blocked` 量的是运行）。
        self._semantic_block_sent: bool = False
        #: 本次运行里 L3 真的跑了几次（模型调用次数，读数用）。
        self._semantic_runs: int = 0
        #: 目标原文（L2.4 判「这次任务是不是优化/提速类」要用它）。
        self._goal_text: str = ""
        # 本次运行里「没有证据的完成声明」条数（探针的 `evidence_gaps` 字段）。
        # **它只进报告与事件流，不改 `finished`**（P0 的纪律）。
        self._evidence_gaps: int = 0
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
        # 本轮运行重置护栏状态：上一次 run() 看到的调用对象/清单指纹与上下文版本
        # 不能带过来（带了的话，第一次重复调用就会拿着旧 run 的状态做判断）。
        self._trace = trace
        self._context_revision = 0
        self._seen_at_revision.clear()
        self._observed_state.clear()
        self._refreshes_used = 0
        # 「组合停用」的计数与留给督导的证据同样是**本次运行**的：
        # 上一次 run() 停用过的组合不该把这一次的第一个同名调用直接拒掉。
        self._blocked_combos.clear()
        self._reroute_evidence = ""
        # 机械审查的去重集合与证据缺口计数也是**本次运行**的：上一次 run() 发过
        # 的那几条不该把这一次的同类发现压掉。
        self._reviewed_notes.clear()
        self._evidence_gaps = 0
        # L2 的对照运行读数同样是**本次运行**的：上一次那批改动的读数不能
        # 当成这一次的（缓存键含内容指纹，但空键会让「这一轮没改动」命中旧报告）。
        self._behavior_report = None
        self._behavior_key = ""
        # L3 的读数同样是**本次运行**的：上一次那批改动的审查结论不能带过来
        # （缓存键含内容指纹，但空键会让「这一轮没改动」命中旧报告）。
        self._semantic_report = None
        self._semantic_key = ""
        self._semantic_decision_sent = False
        self._semantic_block_sent = False
        self._semantic_runs = 0
        # 目标原文（L2.4 判「优化类任务」要用它）。**必须放在这一轮的状态重置
        # 之后**：写在前面的 `goal = state.goal` 那里会被下面这行清零，于是
        # 「优化类任务」永远判不出来——实测就是这样漏掉的（探针里
        # `vague-optimize` 的 L2.4 整条没跑，报告只说「入口跑不完」）。
        self._goal_text = goal
        # 风险分级放行的状态同样是**本次运行**的（§3.2 的打扰预算按运行算）：
        # 上一次 run() 请示过的触发条件不该把这一次的那一次压掉，被拦下的
        # 动作清单也只能属于同一轮。
        self.escalation_gate.reset()
        self._review_fix_rounds = 0
        self._review_blocked = False
        self._review_block_note = ""
        self._review_fixable = False
        self._review_note_sent = False
        # 本次运行「已经产出过多少处编辑」的镜像，以及上一轮被拦下时那份
        # 「逐条发现」的原文。两者一起回答「它被拦下之后有没有再动手」——
        # 同一批发现连着报两次就不回灌（见 run() 的 done 分支）。
        self._edits_made_now = 0
        self._review_last_block = ""
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
            # 同一个组合刚被停用（`_refuse`）：这件事**不再让它自己试**。
            # 机械层已经把参数逐字摆到它面前过，模型对同一句话是免疫的
            # （实测同参连拦 3 次），所以这里直接把轨迹交给督导，带上
            # 「已被拦几次 + 已经给过哪些替代方案」，让它给判断——而不是让它
            # 自己空转到步数上限。
            reroute = self._reroute_evidence
            stuck_on_reroute = self.config.supervise and bool(reroute)
            if (
                state.step >= limit
                or stuck_on_repeat
                or stuck_on_roam
                or stuck_on_explore
                or stuck_on_cycle
                or stuck_on_oscillation
                or stuck_on_query
                or stuck_on_failed_repeat
                or stuck_on_reroute
            ):
                # 两个触发点，同一件事：机械层已经说了有事，而模型自己没纠正。
                # 这是**要判断**的点，不是失败点——原先的处理是停下来提示用户
                # `--resume`，那等于把判断成本推给用户，还把一件事拆成两次对话。
                exhausted = state.step >= limit
                if exhausted:
                    asking = "这一轮的步数预算用完了，任务还没结束"
                elif stuck_on_reroute:
                    # 这条问题与下面几条都不同：这里已经**不是**「它会不会换」
                    # 的问题——机械层把参数逐字端回去了，它还是原样发。所以要
                    # 问的是「这个组合停用之后，这件事还该不该做、怎么做」。
                    asking = (
                        f"同一个（工具 + 参数）组合已经被硬拦并停用——{reroute}。"
                        "它仍然在这一步上反复，机械层已经不再接受这个组合；"
                        "请判断：这件事还该不该做、该换哪条路，或者该收尾。"
                    )
                    self._reroute_evidence = ""
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
                        done_text,
                        state,
                        trace,
                        changed_paths,
                        # 督导判「已完成」也要过「不许宣称完成」那一关：这一条
                        # 路径原先 `gate` 默认 False，于是**督导说完成就绕过了
                        # 审查**——实测（探针 set5 的 `vague-optimize-r1`）L2 报出
                        # 高严重度「变快但结果坏」，`final_ok` 仍是 True。机器
                        # 拦的正是这句声明，谁说的都不例外（§3.1）。
                        gate=True,
                        announce_block=True,
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
                # 版本号 +1，重复调用护栏据此放行刷新读取（见
                # `_guard_repeat_decision` 的第一条判据）。
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

            edits_before_step = edits_made
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
                        # 给收尾闸门一个可读的读数：报告与 trace 里要能回答
                        # 「它做到第几处、被拦下时已经产出过多少」。
                        self._edits_made_now = edits_made
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
                            # 风险分级放行：先记下这个文件**改动前**的公开符号
                            # 快照（T1 的对照），再登记这次编辑命中的路径条件
                            # （T2/T3/T4/T6）。**两者都只记录、不拦**——文件
                            # 编辑是成果，成果照落盘（§3.2.0）。
                            #
                            # 取的是**磁盘原文**、不是 `read_file` 那个叠加了
                            # pending 的视图：走到这一行时 `propose()` 已经
                            # 把新正文放进待确认清单，视图给的是**改之后**的
                            # 内容——拿它当「改动前」，每个删掉的函数都判不出来
                            # （T1 的冒烟一开始就是这么漏的）。
                            self.escalation_gate.snapshot_symbols(
                                path, self._disk_text(path)
                            )
                            for record in self.escalation_gate.record_edit(call):
                                self._log(
                                    f"风险分级放行（只记录不拦）：{record.trigger} "
                                    f"{record.path}——{record.detail}"
                                )
                                self._emit(
                                    "note",
                                    {
                                        "text": (
                                            f"命中 {record.trigger}"
                                            f"（{record.detail}）：成果照落盘"
                                        ),
                                        "ok": True,
                                        "kind": AUDIT_KIND,
                                        "trigger": record.trigger,
                                    },
                                )
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
                    # **没有验证器 ≠ 验证成功**（问题⑤）：那一句是「这次压根没有
                    # 可跑的判据」，把它报成「自动验证 → 成功」，用户读到的就是
                    # 「验过了」——而实际上什么都没验。
                    #
                    # **这一档在步骤里不再单独发一条**（残渣之三）：它是收尾那条
                    # `最终验证` 阶段行的读数（`no_checker` 字段），而结论段另有
                    # 一句 `NO_CHECKER_LINE`。步骤里再发一条 note，用户就在同一屏
                    # 上把「没有校验器（不是失败）」读两遍——现场夹具里那一句还
                    # 顺带把系统对模型说的话一起漏了出来。
                    #
                    # 判据取**验证器的原话**（`_last_verify` 的第 0 项），不是
                    # `report`：后者是回灌给模型的那段话，它已经把「没有对应的
                    # 自动验证器」改写成「没有校验器（不是失败）」，拿它判会永远
                    # 判不出来（这一步曾经踩过：整档退回「自动验证 → 成功」）。
                    raw = self._last_verify[0] if self._last_verify else report
                    no_checker = _says_no_checker(raw)
                    # 验证单独计一笔：它是**起子进程跑测试**，优化方向
                    # （换更快的测试、只跑相关文件）与工具那条完全不同。
                    verify_seconds += time.time() - _verify_started
                    trace.append(
                        f"step{state.step}: 自动验证 -> "
                        f"{VERIFY_SKIPPED if no_checker else ('通过' if passed else '失败')}"
                        f"（累计 {len(scope_paths)} 处改动，"
                        f"{time.time() - started:.1f}s）"
                    )
                    if not no_checker:
                        # detail 给**验证器的原话**：`report` 是回灌给模型的那段
                        # 反馈，末尾挂着第二人称指令，搬到用户可见面上就是泄漏。
                        self._emit(
                            "tool",
                            {
                                "name": "自动验证",
                                "ok": passed,
                                "detail": _verify_event_detail(raw),
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
                    # L1 机械一致性（残留引用 / 删除引用 / 测试改弱）：与验证同一个
                    # 出口、同一批改动，但**只发事件流与报告**，不进模型上下文，
                    # 也不阻断（P0）。`interim=True`：引用闭合留到收尾判终态。
                    self._mechanical_review(scope_paths, interim=True)
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
            # 判据是**整步**的：这一步里的每一个调用都没执行（`blocked_all`），
            # 才叫「没有推进」。工具名一律不看——名单漏一个名字不会有报错，
            # 只会让某个工具的被拦动作继续白吃预算（这正是改前
            # `find_symbol` 的形状）。空调用的一步不算（`turn.tool_calls`
            # 非空才可能「全部被拦」）。
            #
            # 退还必须**看得见**：trace 给督导看，事件流给探针与人看
            # （`kind=blocked_refund`，带次数与上限），否则「这次因被拦而退还」
            # 只在内存里发生过。退还到上限之后照常计预算。
            refund_cap = int(self.config.limit("blocked_refund_limit"))
            blocked_step = blocked_all and bool(turn.tool_calls)
            refunded = False
            if blocked_step and refunds_used < refund_cap:
                refunds_used += 1
                refunded = True
                line = (
                    f"step{state.step}: 这一步的调用全部被硬拦（没有执行），"
                    f"不占步数预算（已退还 {refunds_used}/{refund_cap}）"
                )
                trace.append(line)
                self._emit(
                    "note",
                    {
                        "text": line
                        + f"；被拦的组合：{', '.join(sorted({c.name for c in turn.tool_calls}))}",
                        "ok": True,
                        "kind": _BLOCKED_STEP_KIND,
                        "count": refunds_used,
                        "limit": refund_cap,
                        "tools": sorted({c.name for c in turn.tool_calls}),
                    },
                )
            elif blocked_step:
                # 上限用完了也要如实说：不然「这一步为什么又开始吃预算」看不出来。
                trace.append(
                    f"step{state.step}: 这一步同样全部被硬拦，但退还给到上限"
                    f"（{refunds_used}/{refund_cap}），照常计一步预算"
                )
            if not refunded:
                state.step_forward()
                _state_started = time.time()
                save_state(state, checkpoint)
                state_seconds += time.time() - _state_started

            # 这一步开始时已经产出过多少处编辑。自动修那一关要看「拦住它的
            # 那一步有没有产出」（同一步里既改东西又宣告完成的不给自动修：
            # 那一轮它已经动过了，判词进的是运行报告）。
            if turn.done:
                trace.append(f"step{state.step}: 完成")
                # 它自己宣告完成时也一样要报最终状态：用户点名的第 7 条
                # （收手与落盘抢跑）不区分「谁收的手」。已经落成的改动、
                # 最终文件的真实行数与指纹，都附在结论后面。
                #
                # `gate=True`：**宣称完成那一刻判「许不许宣称」**（§2.6 第 2 条，
                # `turn.done` 之前那一处）。判出高严重度发现时先**自动修有限轮**
                # （轮数走标定值），修不掉才报「未完成 + 逐条 file:line」。
                final_text = self._with_final_state(
                    turn.final or "",
                    state,
                    trace,
                    changed_paths,
                    gate=True,
                    announce_block=True,
                )
                # ---- 自动修有限轮（§3.1 的处理顺序第 1 条）--------------------
                #
                # 「先按 §3.1 的处理顺序自动修 N 轮，修不掉才升级」：这里是那
                # N 轮。**轮数有上限**（`_review_fix_limit`），而且只在有东西
                # 可修时（高严重度发现逐条带 `file:line`）才给——纯规模条件
                # 再问一次也修不掉。
                #
                # 给哪一次？**这一轮的发现与上一轮不一样**的那一次
                # （`same_again` 为假）。两次报的是同一批发现 = 它被拦下之后
                # 什么都没动，再回灌一遍只是复读，会一路烧到步数上限（实测
                # 见过）；发现变了才说明它在按发现逐条修，值得再给一轮。
                same_again = self._review_block_note == self._review_last_block
                if (
                    self._review_fix_limit > 0
                    and self._review_blocked
                    and self._review_fixable
                    and not same_again
                    and self._review_fix_rounds < self._review_fix_limit
                ):
                    self._review_fix_rounds += 1
                    findings = self._review_block_note
                    self._review_last_block = findings
                    self._review_blocked = False
                    self._review_block_note = ""
                    ask = (
                        f"审查拦下了「完成声明」（第 {self._review_fix_rounds}/"
                        f"{self._review_fix_limit} 轮自动修）。这些发现必须先处理掉，"
                        "或者给出机械可核对的理由说明它们不成立：\n"
                        + _flat_line(findings)[:900]
                        + "\n**注意**：这一轮的改动已经落在工作区里，不用重做；"
                        "要修就按上面的 `文件:行` 逐条修。"
                    )
                    feedback = ask
                    trace.append(
                        f"step{state.step}: 完成被审查拦下，回灌逐条发现让它自动修"
                        f"（{self._review_fix_rounds}/{self._review_fix_limit}）"
                    )
                    history.append(Message(role="user", content=ask))
                    continue
                self._archive(state, "success", trace, turn.final or "")
                clear_state(checkpoint)
                # **有动作没执行**（挂起待批）时不许宣称完成：`finished=False`
                # 在这里的含义是「有动作在等批复」，**不是**「成果没落盘」——
                # 成果已经按 §2.5 落在工作区里了（§6 P1 验收口径明确反向过
                # 旧判据：`settled != "auto"` 只表示有动作没执行）。
                settled_clean = not self.escalation_gate.pending_actions
                return LoopResult(
                    finished=not self._review_blocked and settled_clean,
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
                    verified=self._final_verify[0] if self._final_verify else None,
                    verify_summary=self._final_verify[1] if self._final_verify else "",
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
        final = self._with_final_state(
            final,
            state,
            trace,
            changed_paths,
            gate=True,
            # 收手那一刻也要把它宣告出来：这一条路径原先 `announce=False`，
            # 于是「被审查拦下」这件事只进了账本、没进结论，而收手报告正是
            # 人读的那一份（§3.1 第 2 条：未命中触发条件时同样 `finished=False`，
            # 结论写成「未完成（被审查拦下）+ 每条发现 + file:line」）。
            announce_block=True,
        )
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
            verified=self._final_verify[0] if self._final_verify else None,
            verify_summary=self._final_verify[1] if self._final_verify else "",
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

    def _undo_command(self) -> str:
        """收尾报告里那一行**可直接复制**的撤销命令。

        为什么非得有这一行（真实经历）：用户接受了一份「最终验证未通过」的改动，
        事后反悔，用系统外的手段（自己找备份 / 编辑器）退了回去——而
        `spool revert` 明明存在。可发现性不是文档问题：报告说了「共落成 N 处
        改动」，却没说「退掉它们的那条命令是什么」，人只能自己想办法。

        没有改动就不给：一条跑不通的命令比不给更坏——用户会去跑它，然后
        得到「没有这条记录」，从此不再相信这个入口。
        """
        if self.pending is None:
            return ""
        landed = getattr(self.pending, "landed", None)
        if not len(self.pending) and not (landed() if landed else []):
            return ""
        return f"spool revert --run {self.pending.run_id}"

    def _collect_report(
        self,
        final: str,
        changed: Sequence[str],
        facts: str,
        outcome: str,
        boundary: str,
        *,
        passed: bool | None,
        body: str,
        ledger: str = "",
    ) -> str:
        """拼收尾报告：**结论第一、详情另成一行**。

        五段（顺序就是这条要求本身，问题①②③④⑤⑥⑦都落在这里）：

            ① 结论 —— 模型那一句（做了什么 / 结果），独立成行
            ② 详情 —— 落成计数 + 文件 + 行数 / 指纹（`facts`，带 `详情：` 标记）
            ③ 验证 —— 一句：通过 / 未通过 / **没有校验器（不是失败）**
            ④ 风险 / 被拦 —— **只在真的命中时出现**（L2 发现、高危发现、请示）
            ⑤ 可做的动作 —— **一次**，且写成动作（撤销那条命令照旧逐字给出，
               既有验收里有用例把它抄下来真执行）

        **② 是详情，不是结论段**（残渣之二）：落成计数 / 文件状态 / 指纹是账目，
        它们带 `详情：` 标记，网页壳据此折进「思考过程」（`web/runner.py::split_report`）。
        所以①③④⑤ 是结论段，② 单独一行、单独一档——**不再与 ③ 并在同一行**
        （并进 ③ 之后详情就没有自己的行了，渲染层无从把它折走）。

        读报告的人要的只是「做成了没有 / 动了什么 / 验没验 / 有事没有 /
        我能做什么」，不是实现账本。

        `passed is False` 时**判词、失败要点与退路回到最前面**（既有验收）：
        大 diff 抢眼，判词排在它后面等于没说。

        `ledger` 是证据账本的降级那一句（没有缺口时为空串）：它紧跟验证
        结论，因为「通过 / 未通过 / 未验证」是同一句话的三个取值。
        """
        undo = self._undo_command()
        no_checker = _says_no_checker(body)
        head = ""
        if passed is False:
            # ③ 未通过那一句 + 失败要点 + ⑤ 退路：三者一起挪到最前，而且**只在
            # 这里出现一次**（下面正文里不再重复「最终验证：未通过」与撤销命令）。
            head = (
                "⚠ 最终验证：未通过——这一轮的改动没有通过验收。\n"
                f"失败要点：{_flat_line(body)[:300]}\n"
                + (f"{_undo_action_line(undo)}\n" if undo else "")
                + "（上面是判词与退路，下面才是这一轮的结论与文件状态。）\n"
            )
        # 「不许宣称完成」的判词必须排在模型那段之前：模型那段常常就是一份
        # 大 diff，埋在下面等于没说什么（与最终验证未通过同一处理由）。
        # 判词末尾**必须留一个换行**：不留的话模型那一句会挂在判词的末行上，
        # 「文件建好没有」这个答案就跟「被拦下、等批复…」糊成同一行（问题⑦
        # 那种"答案被埋"的形状之一）。
        if self._review_blocked:
            head = self._review_block_note + "\n" + head
        # ---- ② 详情（逐条：文件 + 行数 / 指纹）--------------------------------
        #
        # 行首那个标记是给渲染层的（`DETAIL_MARK`）：账目还在报告里（终端那条路
        # 照样读得到），只是不再挤在结论段里。
        facts_line = facts or "最终文件状态：这次运行没有改过任何文件。"
        if changed:
            facts_line = f"本次运行共落成 {len(changed)} 处改动。{facts_line}"
        detail_line = DETAIL_MARK + facts_line
        # ---- ③ 验证一句 -------------------------------------------------------
        if passed is None:
            # 没挂验证器：`_with_final_state` 给的就是那一句，原样用。
            verify_line = outcome
        elif no_checker:
            # 结论段这一句回答"这次验没验"；过程区那一档状态字
            # （`VERIFY_SKIPPED`，例「最终验证 → 跳过（不是失败）」）回答
            # "这一步真的跑了吗"。两句**刻意不同**：同一句出现两次就是残渣之三。
            verify_line = NO_CHECKER_LINE
        else:
            brief = _verify_brief(body)
            verify_line = (
                ("最终验证：通过" if passed else "最终验证：未通过")
                + (f"——{brief}" if brief else "。")
            )
        # ---- ④ 风险 / 被拦（**仅命中时**出现）--------------------------------
        risk = self._behavior_report_section(changed)
        if boundary:
            risk += boundary
        attention = self._escalation_note()
        if attention:
            risk += attention
        # ---- L3 那一段：逐条 file:line（只在真的有东西时出现）----------------
        semantic = self._semantic_report_section()
        if semantic:
            risk += semantic
        # ---- 组装：结论 / 验证 / 风险 / 详情 / 动作 ---------------------------
        #
        # 原先"小改动成功"时把 ②③ 并成一行（压到三行）：那一步正是把内部账糊到
        # 结论段上的最后一手——并进 ③ 之后，「详情」就没有自己的行了，渲染层
        # 无从把它折走（网页壳只能照单全收）。现在一行一段，详情排在动作之前。
        lines: list[str] = [str(final or "")]
        if passed is not False:
            # 未通过那一句已经在最前面说过一次了：同一信息不出现两次。
            lines.append(verify_line)
        if ledger:
            lines.append(ledger)
        if risk:
            lines.append(risk)
        lines.append(detail_line)
        # ---- ⑤ 可做的动作（一次）---------------------------------------------
        if undo and passed is not False:
            lines.append(_undo_action_line(undo))
        return head + "\n".join(lines)

    def _with_final_state(
        self,
        final: str,
        state: TaskState,
        trace: list[str],
        changed: Sequence[str],
        *,
        gate: bool = True,
        announce_block: bool = False,
    ) -> str:
        """给收尾结论补上「最终文件真实验证结果 + 真实状态」。

        收手时也要对最终文件跑一次验证（有验证器时）。结果**如实**附在后面：

        - 改动确实落成了 → 「改动已落成」+ 验证结果，不许说成「0 处改动」；
        - 文件在收手时是坏的 → 说出来；收手判词不改写这件事。
        - **验证未通过时，那句判词与撤销命令要出现在最前**（见 `_collect_report`）。

        **一处改动都没有时不加这段**。理由有两条，都不是省事：① 「这次运行
        没改过任何文件」对一个只读角色（审查者）是纯粹的噪声，每一句结论后面
        都拖着它；② 更要紧的是它会被读成「系统在替我下结论」。真正要防的是
        「有落盘却被判词抹掉」，而那种情况必然有改动——所以只在有改动时段。

        `gate` **默认开**：「不许宣称完成」那一关是收尾出口的**必须件**
        （§3.1），任何一条收尾路径都不该靠「记得传这个参数」来保证它跑。
        实测这条默认值踩过坑：督导说「已完成」的那条路径原先拿到的是默认关，
        于是 L2 报出高严重度「变快但结果坏」时 `final_ok` 仍然是 True——机器
        拦的正是那句声明，谁说的都不例外。
        """
        if not changed:
            return final
        facts = self._file_state_line(changed)
        # L1 机械一致性在收尾这一刻再跑一遍：模型最后几步可能又削弱了测试、
        # 又留下了残留。去重集合保证同一条不会重复上报（P0：只报不阻断）。
        #
        # **这里是「重判」而不是「再判一遍」**：账本只累加不清，而判词是就
        # **某一版内容**下的结论——中途那一版有残留、模型随后改好了，旧判词
        # 还挂在账本里，收尾报告与 T12（判据原话是「仍在」）就会拿一条**已经
        # 不成立**的判词去拦完成声明。所以先按终态内容重算，再拿它替换旧判词。
        self._rejudge_mechanical(list(changed))
        # ---- L2 行为回归（P2，§5 L2）------------------------------------------
        #
        # 位置在这里是**必须的**：下面两条收尾出口（「内容未变、沿用上次验证」
        # 的捷径与本体）都要过「不许宣称完成」那一关，而 L2 的发现是那一关的
        # 输入之一。放在 `_apply_completion_gate` 之后，发现就进不了这一次判定。
        #
        # 它**不动落盘、不回滚任何东西**：成果已经（或即将）落在工作区里，
        # 这里判的是「同一条命令、同一份输入，输出有没有变」与「优化类任务
        # 是不是真的做到了又快又不变」（§2.5：拦的是完成声明，不是成果）。
        self._behavior_review(list(changed))
        # ---- L3 语义审查（P2 后半，§5 L3）-------------------------------------
        #
        # 与 L2 放在同一个位置、同一个理由：它是「不许宣称完成」那一关的输入
        # 之一，必须排在 `_apply_completion_gate` 之前（下面两条收尾出口都要过
        # 那一关）。
        #
        # 与 L2 的两点不同，都是这一层的成本纪律：
        #   ① 它只在**触发条件满足**时真的调用模型（`decide_triggers`），
        #      不满足时一次调用都不发生（§5、§7.5「不做成每步都跑」）；
        #   ② 它对**同一份内容**只跑一次（缓存键与 L2 同口径），而自动修的
        #      每一轮内容都会变，所以那一轮会重跑一次——那正是「发现当反馈
        #      重新注入下一轮」要的（§3.1 处理顺序第 1 条）。
        #
        # 它同样**不动落盘、不回滚任何东西**：高严重度发现只拦「完成声明」与
        # 危险动作，成果照落盘（§2.5 的硬不变量）。
        #
        # **传进去的是「上一版内容的验证结论」**（`_final_verify`），不是这一版
        # ——这一版的验证还在下面几步之后才跑（那个 `passed` 变量此刻还是 None）。
        # 证据账本的缺口判据对它敏感：拿 None 去算的话**每一轮都会报一条「没跑」
        # 的缺口**，于是 L3 在小改动上也永远触发——那是「每步都跑」的另一种写法
        # （实测抓出来的：一次 `note.txt` 单文件小改也被判成 F4）。第一次收尾时
        # 这里为 None，账本如实说「没跑」并按 F4 触发一次，那是**对的**：
        # 那一刻确实还拿不到结论。
        prior_verified = self._final_verify[0] if self._final_verify else None
        self._semantic_review(list(changed), prior_verified=prior_verified)
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
            # 「沿用那一次的结果」是**内部账**（问题④）：它是实现细节（为什么
            # 这一次没有重跑），用户读的那一份里不需要它——报告只说结论。
            # 它在 trace 与 `最终验证` 那条事件里照旧留着，排错时能查到。
            outcome = ("最终验证：通过。" if passed else "最终验证：未通过。") + body
            no_checker = _says_no_checker(body)
            trace.append(
                "收尾: 报出最终文件的验证结果 -> "
                f"{VERIFY_SKIPPED if no_checker else ('通过' if passed else '未通过')}"
                "（内容未变，沿用最近一次）"
            )
            payload = {
                "name": "最终验证",
                "ok": passed,
                # 第三档（问题⑤）：`ok=True` 说的是「没有发现问题」，不是
                # 「验过了」。网页壳据此渲染成「最终验证 → 跳过（不是失败）」
                # 而不是「成功」；理由由 detail 给（验证器的原话）。
                "no_checker": no_checker,
                "detail": _verify_event_detail(body),
            }
            if no_checker:
                # 第三档的**机械读数**（`kind`）：它现在挂在这一条阶段行上
                # （步骤区不再另发一条 note），MCP 驱动者与探针按它取。
                payload["kind"] = NO_CHECKER_KIND
            self._emit_final_verify(payload)
            self._final_verify = (passed, body)
            self._note_verification(passed, body)
            if gate and changed:
                self._apply_completion_gate(changed, announce=announce_block)
            return self._collect_report(
                final,
                changed,
                facts,
                outcome,
                boundary,
                passed=passed,
                body=body,
                ledger=self._evidence_ledger(changed, passed).downgrade_note(),
            )
        passed: bool | None = None
        body = ""
        if self.verify is not None:
            report, passed = self._run_verification(list(changed))
            # 报告那一段用**验证器的原话**（`self._last_verify`），不是回灌给
            # 模型的那层壳（「系统自动跑了一遍测试：通过（…）」）：两者只差
            # 外面那句解释，而报告里不需要解释给模型听。这也让「沿用」那条
            # 捷径与这里用的是同一种正文（那条给的就是 `_last_verify`）。
            body = self._last_verify[0] if self._last_verify else report
            outcome = ("最终验证：通过。" if passed else "最终验证：未通过。") + body
            no_checker = _says_no_checker(body)
            trace.append(
                f"收尾: 对最终文件跑了一次验证 -> "
                f"{VERIFY_SKIPPED if no_checker else ('通过' if passed else '未通过')}"
            )
            payload = {
                "name": "最终验证",
                "ok": passed,
                # 与上面那条「沿用」的出口同一个口径：没有校验器是第三档。
                "no_checker": no_checker,
                # 事件流那一份给**验证器的原话**（`body`，不是回灌给模型的那段
                # `report`）：后者末尾挂着第二人称指令，是给模型看的。
                # 没有校验器时 `_verify_event_detail` 只留理由那一句。
                "detail": _verify_event_detail(body),
            }
            if no_checker:
                payload["kind"] = NO_CHECKER_KIND
            self._emit_final_verify(payload)
            self._final_verify = (passed, report)
            self._note_verification(passed, report)
        else:
            outcome = "最终验证：本次运行没有挂验证器，未跑。"
        # ---- 风险分级放行 + 不许宣称完成（§3.1 / §3.2）------------------------
        # 位置在这里是必须的：最终验证结论刚刚拿到（T8 的判据要它），而收尾
        # 报告还没拼（那一段要排在报告前面）。**这一步不回滚任何东西**：成果
        # 已经落在工作区里，这里拦的只是那句「完成」（§2.5）。
        #
        # **两条出口都要过这一关**（上面那条「内容未变、沿用上次验证」的
        # 捷径也一样）：那条捷径原先在这之前就 return 了，于是「上一步刚验过
        # 同一批内容」的运行会绕开闸门——实测第三个冒烟场景就是这么漏过去的。
        if gate and changed:
            self._apply_completion_gate(changed, announce=announce_block)
        return self._collect_report(
            final,
            changed,
            facts,
            outcome,
            boundary,
            passed=passed,
            body=body,
            ledger=self._evidence_ledger(changed, passed).downgrade_note(),
        )

    def _apply_completion_gate(
        self, changed: Sequence[str], *, announce: bool = True
    ) -> None:
        """判一次「许不许宣称完成」，并把它记进 trace 与事件流（§3.1）。

        `announce=False` 是**自动修那一轮**：判据照跑（触发条件照样请示、账本
        照样记），但那句「⚠ 未完成（被审查拦下）」不拼进这一轮的正文——
        它会被回灌给模型，而模型看到「未完成」会去补做已经做完的事（实测：
        rename 由 7 步涨到 10 步、最后撞上步数上限）。**只在真正收尾那一刻
        宣告**，这正是 §3.1「不许宣称完成」要拦的那句话。
        """
        blocked = self._completion_blocked(changed)
        if not blocked:
            return
        self._review_blocked = True
        self._review_block_note = blocked if announce else ""
        # ---- L3 拦下完成声明（§3.1 第 3 条 / §6 的 `l3_blocked`）--------------
        #
        # **一次运行只发一条**（与 `blocked_by_review` 同一个理由：探针那个字段
        # 量的是运行，不是「判了几次」）。它只说明「L3 的高严重度发现让结论降级」
        # ——**不是**「成果没落盘」：那两件事在本设计里已经分开（§6 字段表）。
        if not self._semantic_block_sent and self.semantic_high_findings():
            self._semantic_block_sent = True
            report = self._semantic_report
            if report is not None:
                self._emit("note", semantic_blocked_payload(report))
        trace = self._trace
        if trace is not None:
            trace.append("收尾: 审查拦下了「完成声明」——" + _flat_line(blocked)[:160])
        # **一次运行只发一条**：自动修的每一轮都会重算这件事，每轮发一条的话
        # `blocked_by_review` 会等于轮数（rename 那条实测 3），而它与 `check_ok`
        # 一起派生的 `blocked_but_ok` 就变成误报源——那个字段量的是「被阻断
        # 但验收绿」的**运行**数，不是「判了几次」（§6 字段表）。
        if self._review_note_sent:
            return
        self._review_note_sent = True
        # 探针按这个 kind 数 `blocked_by_review`。**它量的是「结论被降级」，
        # 不是「成果没落盘」**：那两件事在本设计里已经分开
        # （§6 字段表对 `blocked_by_review` 的括注）。
        self._emit(
            "note",
            {
                "text": blocked,
                "ok": False,
                # **两条 kind 分开**：`blocked_by_review` 记「拦了一次」，
                # `..._finding` 记「拦它的理由是 L1/L2 真的报了高严重度发现」。
                # 探针用后者算 `blocked_but_ok`（误报代价）：只把它算在
                # **机械判据报了真缺陷**的那一类上，否则「验收判据看不见、
                # 而机械判据看见了」的真缺陷会被记成误报，那个数字永远非零。
                "kind": (
                    BLOCKED_REVIEW_HARD_KIND
                    if self._review_fixable
                    else BLOCKED_REVIEW_KIND
                ),
                "triggers": sorted(
                    {item.trigger for item in self.escalation_gate.all_actions}
                ),
                "has_finding": bool(self._review_fixable),
            },
        )

    def _note_verification(self, passed: bool, summary: str) -> None:
        """把最终验证结论交给待确认清单。

        落盘那一刻它会被写进运行记录（`.agent/runs/<id>/run.json`）——
        整轮退回去之后再回头看这份记录，要能读到「这一轮当初是什么结论」。
        """
        note = getattr(self.pending, "note_verification", None)
        if note is not None:
            note(passed, summary)

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

        **重复调用按机械判据分成三类**（判据全部在 `_guard_repeat_decision`，
        这里只说处置）——这一轮用户点名的两条真病态就死在第一条上：

        - **无用重复**（参数逐字相同 + 该对象没有可观察变化 + 上一次结果还在
          上下文里）：**第一次重复就硬拦 + 换路**。以前这里只是「提醒一句、
          照常执行」，那一次真的又跑了一遍、白烧一次完整的模型生成与工具执行
          ——它记在「空转」账上（探针的 `repeat_blocks_unblocked`）。
          注意判据是**两件事都成立**才拦：刚写完再读一遍那种合法的重取，
          内容指纹已经变了，走的是下面那一条，不会被误拦。
        - **有理由的重取**：内容指纹变了，或上下文版本推进导致原结果已被压掉
          → **放行**，并把理由写进 trace 与事件流（`kind=guard_refresh`）。
          它不是空转：模型没有别的办法拿到新内容。
        - **判不出**（拿不到指纹、无法比较，例如 `run_command`）：保留「提醒」
          路径放行，但**记下「这次判不出」**（`kind=guard_repeat_undecided`）
          ——不许假装判过。

        放行有上限：`_REFRESH_LIMIT`，否则「不断被拦」+ 硬拦退还预算就成了
        无限预算。
        """
        signature = call_signature(call)
        # ---- T6：写工具的目标越过工作区根（§3.2.2 的分流）--------------------
        #
        # 分流的判据是**路径落在哪儿**，两条路的处置完全不同（Lead 的裁决）：
        #
        # - **已授权关联目录里**（`spool extra add` 加过）：那是「读放开，
        #   **写要确认**」的语义。所以拦下这次写、请示一次；没批准就不执行
        #   它，而这一轮其余成果照落盘（§2.5 的硬不变量）。以前这里要么按
        #   策略静默丢掉、要么在无人值守时连整批一起丢——那条语义就没兑现。
        # - **工作区与所有关联目录之外**：保持硬拒绝，但把「怎么才能做」与
        #   「为什么不问」写进话术（`outside_write_refusal`）。
        boundary = self.escalation_gate.boundary_targets(call)
        for kind, label in boundary:
            if kind == OUTSIDE:
                self._log(f"T6 域外写直接拒绝：{label}")
                # 这一条**不进**「组合停用」的计数（`_refuse`）：它的原始话术
                # 里那段「怎么才能做」是这条路的全部价值，换成清单式说法会把它
                # 挤掉。而且它是**策略**拒绝（这件事不允许），与「重复没有意义」
                # 不是同一个问题——两者混在一起，停用清单会开始说错理由。
                return ToolResult(ok=False, content=outside_write_refusal(label))
        # ---- 风险分级放行：**动作执行口之前的拦截**（§2.6 第 2 条）------------
        #
        # 位置就在这里（`registry.invoke` 与刷新放行都在它后面）：判据是
        # 「命中 T1–T13 → 在执行那个动作前拦下并请示」（§3.2.2）。文件编辑
        # 不会走到这里——`action_triggers` 对写工具**永远**返回空表，它们是
        # 成果，成果照落盘（§3.2.0）；**唯一**让写工具进闸门的就是上面那条
        # T6 的关联目录分流。
        gate_requests = list(self.escalation_gate.action_triggers(call))
        for kind, label in boundary:
            if kind == LINKED:
                gate_requests.insert(0, self.escalation_gate.boundary_action(label))
        for request in gate_requests:
            self._emit(
                "note",
                {
                    "text": f"危险动作进入请示：{request.action}（{request.trigger}）",
                    "ok": False,
                    "kind": ACTION_BLOCK_KIND,
                    "trigger": request.trigger,
                    "gated": True,
                    "fingerprint": request.fingerprint,
                },
            )
            decided = self._escalate(request)
            decision = self.escalation_gate.decisions.get(decided.fingerprint)
            if decision is None or not decision.execute:
                return self._refuse(
                    call,
                    signature,
                    (
                        f"{BLOCKED_PREFIX}这个动作被风险分级放行拦下了（触发条件 "
                        f"{request.trigger}，{request.note}）。**成果不受影响**——"
                        "这一轮已经落盘的改动仍在工作区里，回退命令见收尾报告。"
                        f"{_reverse_clause()}"
                        "要往下做就换一条不需要该动作的路（改代码而不是跑那条命令、"
                        "小步改而不是批量删），或者等人批复。"
                        + (
                            f"\n{_intent_clause(call)}" if call.intent else ""
                        )
                    ),
                )
        failures = self._failed_calls.get(signature, 0)
        block_at = max(int(self.config.limit("failed_repeat_block_at")), 2)
        if failures and failures >= block_at - 1:
            failure = self._failed_output.get(signature, "")
            return self._refuse(
                call,
                signature,
                _retry_block_message(call, failure),
                _remedies(failure),
            )
        level = max(repeats, seen)
        # 「无用重复第一次就拦」这条只在**阈值没被重新标定过**时生效。
        #
        # 理由：`repeat_block_at` 是**能力标定值**——标定过它的工作区（或命令行）
        # 明确表达了「这个阈值我用别的数」，那它就该是唯一的线。默认值
        # （3 = 连续第三次不再执行）是按本机小模型实测出来的，而实测表明
        # 它在「第一次重复」这一档上留了一次白跑（两条真病态就是这么来的），
        # 所以默认口径下把机械判据提到第一次重复；显式改过阈值的人拿到的
        # 仍是他要的那个数，不被这条顺带收紧。
        _value, _source = _limits.resolve(
            "repeat_block_at", getattr(self.config, "overrides", None)
        )
        threshold_untouched = _value == _limits.resolve("repeat_block_at")[0]
        gate = level > 1 if threshold_untouched else level >= int(
            self.config.limit("repeat_block_at")
        )
        if gate:
            # ---- 第一次重复就在**判据**上分开（见 _guard_repeat_decision）------
            #
            # 这里以前是「第一次只提醒、第三次才硬拦」。实测的代价是：第一次
            # 重复**真的又跑了一遍**，白烧一次完整的模型生成与工具执行，而它
            # 记在「空转」账上（merge-html 与 fix-bug 各一次，两条真病态）。
            # 现在改成先用机械判据归三类，再按类处置：
            #   无用 → 第一次就硬拦 + 换路；有理由 → 放行并写明理由；
            #   判不出 → 保留提醒路径，但记下「这次判不出」。
            #
            # 判据不许无脑拦：刚写完再读一遍、文件真的变了之后再查一遍，
            # 都是合法的重取（判据二成立 → 有理由的重取）。
            decision, reason = self._guard_repeat_decision(signature, call)
            if decision == REPEAT_USELESS:
                return self._refuse(
                    call,
                    signature,
                    (
                        f"{BLOCKED_PREFIX}这个调用与上一次**完全相同**"
                        f"（工具 {call.name}，参数一字未改），"
                        f"而且{reason}，所以这一次没有执行。"
                        "你这次重发的参数（与上一次逐字相同）："
                        f"{_render_args(call)}"
                        "上一步的输出仍在你的上下文里：换一种做法：换一个工具、"
                        "换一组参数，或者先查别的地方；如果上一步的输出已经够了，"
                        "就基于它往下做（提改动 / 给结论），不要再查一遍。"
                        "下一步的意图要写清「这一次想拿到什么、拿到之后做什么」——"
                        "只重复一句「再看一遍文件」不算下一步。"
                        + _reverse_clause()
                        + (f"\n{_intent_clause(call)}" if call.intent else "")
                    ),
                    _remedies(self._failed_output.get(signature, "")),
                )
            if decision == REPEAT_REFRESH and self._refreshes_used < _REFRESH_LIMIT:
                return self._guard_allow_repeat(
                    call, signature, level, reason, REPEAT_REFRESH
                )
            # 剩下的两类都退回「提醒」路径（照常执行），区别只在事件流与 trace
            # 里留下的**理由**：一类是「判不出」，一类是「判得出有理由但放行
            # 额度用满了」。两者混成一句话，就等于把「没判」说成「判过」。
            if decision == REPEAT_UNDECIDED:
                line = f"⟳ 重复调用判不出：{reason}（保留提醒路径）"
                note_kind = GUARD_UNDECIDED_KIND
                extra: dict = {"decision": decision, "tool": call.name}
                warn_note = (
                    "注意：这一步与上一步完全相同，该调用已经执行过一次，"
                    "结果不会改变。"
                )
            else:
                line = (
                    f"⟳ 重复调用判得出有理由（{reason}），但放行已到上限"
                    f"（{self._refreshes_used}/{_REFRESH_LIMIT}）："
                    "本次只提醒、不再额外放行"
                )
                note_kind = GUARD_UNDECIDED_KIND
                extra = {"decision": decision, "tool": call.name, "capped": True}
                warn_note = (
                    "注意：这个调用在最近几步里已经执行过；"
                    "它这次确实可能拿到新内容，但同一处已经放过几次了。"
                )
            self._log(line)
            self._emit("note", {"text": line, "ok": True, "kind": note_kind, **extra})
            result = self.registry.invoke(call)
            self._note_guard_result(call, result)
            if not result.ok:
                self._remember_failure(signature, result.content)
            if repeats >= self.config.limit("repeat_warn_at"):
                return ToolResult(
                    ok=result.ok,
                    content=(
                        warn_note
                        + "如果它没有帮你推进，就换一种做法。\n"
                        + result.content
                    ),
                )
            return result
        result = self.registry.invoke(call)
        self._note_guard_result(call, result)
        if not result.ok:
            self._remember_failure(signature, result.content)
        elif result.content.startswith(STAGE_MARKER):
            # 正文已暂存：这一次**不是失败**，但「同一份正文再发一遍」是这次
            # 运行里最贵的一种浪费（输出预算就是卡在这里的）。所以也记进失败表：
            # 原样重提会被硬拦，拦下的话术里带着 confirm_rewrite 的调用形状。
            self._remember_failure(signature, result.content)
        # 第一次执行（level == 1）不加任何提醒话术：它本来就不是重复。
        return result

    def _guard_allow_repeat(
        self, call: ToolCall, signature: str, level: int, reason: str, decision: str
    ) -> ToolResult:
        """判明「有理由的重取」之后放行一次，并把理由留在三处可见的地方。

        三处缺一不可：
        - **模型可见文本**：前缀 `REPEAT_REFRESH_PREFIX` + 理由——它得知道自己
          这一次为什么被允许，否则同一个调用会一路发到上限；
        - **事件流**（`kind=guard_refresh`）：探针据此把它与「护栏只提醒、没拦」
          分开——前者是护栏判过的放行，后者才是空转；
        - **trace**：督导看的日志。

        「放行过就要更新它看过什么」这条同样重要：不更新的话，下一次同签名进来
        时判据仍然成立，会连着放行到上限——那是把一次性补救当成了常规通道。
        """
        self._refreshes_used += 1
        trace_line = (
            f"{REPEAT_REFRESH_PREFIX}同一个调用第 {level} 次，"
            f"但{reason}，因此重新执行一次"
            f"（已放行 {self._refreshes_used}/{_REFRESH_LIMIT}）"
        )
        self._log(trace_line)
        self._emit(
            "note",
            {
                "text": trace_line,
                "ok": True,
                "kind": GUARD_REFRESH_KIND,
                "decision": decision,
                "reason": reason,
                "level": level,
                "tool": call.name,
            },
        )
        self._emit(
            "note",
            {
                "text": f"{REPEAT_REFRESH_PREFIX}{reason}",
                "ok": True,
                "kind": GUARD_REFRESH_KIND,
                "decision": decision,
            },
        )
        result = self.registry.invoke(call)
        self._note_guard_result(call, result)
        self._seen_at_revision[signature] = self._context_revision
        intent = _intent_clause(call)
        return ToolResult(
            ok=result.ok,
            content=(
                f"{REPEAT_REFRESH_PREFIX}{reason}，所以这一次重新取了一遍"
                f"（{decision}：已放行 {self._refreshes_used}/{_REFRESH_LIMIT}）。"
                f"{self._file_state_note(call)}"
                + (f"\n{intent}" if intent else "")
                + f"\n{result.content}"
            ),
        )

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

    # ---- 风险分级放行（P1，§3.2）------------------------------------------

    def _note_escalation_finding(self, request: EscalationRequest) -> None:
        """把一条触发条件记进审查账本（它同时是收尾报告的一段）。

        **严重度刻意是「提示」而不是「高危」**（这条不是洁癖）：高严重度发现
        会喂给 `completion_triggers` 的 T12，于是「升级」自己会把升级
        再触发一次——那是自激振荡，不是判据。升级是一次**请示**，它该看见的
        地方是事件流、trace 与收尾报告那一段（`render_decisions`），
        而不是「高危发现」那张表（那张表列的是**问题**）。
        """
        finding = Finding(
            kind=f"escalation_{request.trigger.lower()}",
            severity=INFO,
            summary=(
                f"触发条件 {request.trigger}（{request.note}）：{request.detail}"
            ),
            detail="拦的是动作，不是成果：这一轮的产出照落盘，可回退（§2.5）",
            escalate=True,
            path=request.paths[0] if request.paths else "",
            line=0,
        )
        self.audit_log.add(finding)

    def _escalate(self, request: EscalationRequest) -> EscalationRequest:
        """请示一次。**五件一次给全**，然后按批复通道拿回答。

        返回值上的 `summary` 已经填好五件的第 ①②③④⑤ 四条半（回退由这里
        按 `pending` 现算），调用方拿它去渲染。**同一触发条件不重复打扰**：
        第二次命中只记一条发现，不再问一遍（§3.2.4 的打扰预算）。
        """
        gate = self.escalation_gate
        revert = self._revert_command()
        conclusion = self._escalation_conclusion(request)
        summary = gate.summary_for(
            request, conclusion=conclusion, revert_command=revert
        )
        filled = EscalationRequest(
            trigger=request.trigger,
            action=request.action,
            detail=request.detail,
            summary=summary,
            paths=request.paths,
            before_action=request.before_action,
            fingerprint=request.fingerprint,
        )
        # **同一个动作 / 同一件事在同一轮里只请示一次**（§3.2.4 的打扰预算）：
        # 自动修 N 轮的每一轮都会重算触发条件，不在指纹上收口的话，一次运行
        # 会把同一件事问 N 遍——`escalation_rate` 直接越过失败线，而用户被问的
        # 还是同一个问题。指纹是内容算出来的，所以「同一个动作」判得准：
        # 换了动作（另一条命令、另一处删除）指纹就变，照问。
        answered = next(
            (
                item
                for item in gate.all_actions
                if item.fingerprint == filled.fingerprint
            ),
            None,
        )
        if answered is not None:
            refreshed = gate.refresh(filled)
            self._note_escalation_finding(refreshed)
            return refreshed
        if request.trigger not in gate.escalated_triggers:
            gate.escalated_triggers.append(request.trigger)
        self._note_escalation_finding(filled)
        text = render_summary(filled)
        # 事件流：探针按 `escalate` 数 `escalations`，并读五件齐不齐与摘要指纹。
        # `blocked=False` 表示这一次没有拦下动作（纯结论级升级）——它同样是
        # 「该问就问」，所以照样算一次请示。
        self._emit(
            "note",
            {
                "text": text,
                "ok": False,
                "kind": ESCALATE_KIND,
                "trigger": filled.trigger,
                "action": filled.action,
                "blocked": bool(filled.action),
                "before_action": bool(filled.before_action),
                "summary_complete": bool(filled.complete),
                "fingerprint": filled.fingerprint,
            },
        )
        self._log(f"风险分级放行：{text.splitlines()[0]}")
        if filled.action:
            # 动作事件与请示事件分开记：探针要能核对「先请示、后考虑执行」
            # （`escalated_before_action == escalations`）。
            self._emit(
                "note",
                {
                    "text": f"拦下动作（执行前）：{filled.action}（{filled.trigger}）",
                    "ok": False,
                    "kind": ACTION_BLOCK_KIND,
                    "trigger": filled.trigger,
                    "fingerprint": filled.fingerprint,
                },
            )
        gate.hold(filled)
        word, channel = self._review_word(filled, text)
        approved = parse_reply(word) if word else "revert"
        if not word:
            decision_channel = "unattended"
            approved = "revert"
        else:
            decision_channel = channel or "terminal"
        if filled.action and approved in ("approve", "partial"):
            # 批复「批准」→ 这个动作**不再挂起**：待批清单的定义是「还没执行的
            # 动作」，把批过的留在里面会让收尾报「有动作等批复」而实际已经执行
            # （工具层的批准对它没有意义——它只认 y/n）。「只批准其中几处」按
            # 保守口径留在待批里：那个粒度是**动作**，P1 不假装知道该批哪几处。
            if approved == "approve":
                self.escalation_gate.release(filled)
        decision = EscalationDecision(
            approved=approved,
            word=word,
            channel=decision_channel,
            fingerprint=filled.fingerprint,
        )
        gate.remember(decision)
        self._emit(
            "note",
            {
                "text": (
                    f"批复（{decision_channel}）：{word or '（没人回答）'} → "
                    f"{approved}；不执行这个动作不等于回滚成果，要退成果用回退命令"
                ),
                "ok": approved == "approve",
                "kind": "escalation_reply",
                "trigger": filled.trigger,
                "word": word,
                "channel": decision_channel,
                "approved": (
                    ESCALATION_PENDING if not word else approved == "approve"
                ),
                "fingerprint": filled.fingerprint,
            },
        )
        self._log(
            f"批复来源 {decision_channel}：{word or '（无人值守，不执行该动作）'}"
        )
        return filled

    def _review_word(
        self, request: EscalationRequest, text: str
    ) -> tuple[str, str]:
        """拿一次批复。**注入的通道优先**，没有注入就当无人值守。

        无人值守**不弹问题、也不静默放行**：返回空词，调用方按
        「不执行这个动作、挂起待批」处理（§3.2.3）。
        """
        if self.escalation_review is None:
            return "", ""
        try:
            answer = self.escalation_review(request, text)
        except Exception as exc:  # noqa: BLE001 - 通道坏了按「不执行」处理
            self._log(f"批复通道不可用（{type(exc).__name__}），按不执行处理")
            return "", ""
        if isinstance(answer, tuple) and len(answer) == 2:
            return str(answer[0] or ""), str(answer[1] or "")
        return str(answer or ""), "channel"

    def _escalation_conclusion(self, request: EscalationRequest) -> str:
        """五件的第 ② 件：结论 + 证据（`file:line` / 退出码 / 测试摘要），**不贴 diff**。"""
        bits: list[str] = [request.detail or request.note]
        for finding in self.audit_log.high()[:5]:
            spot = finding.where or "（未给位置）"
            bits.append(f"{spot} {finding.summary}")
        if self._final_verify is not None:
            ok, body = self._final_verify
            bits.append(
                f"最终验证 {'通过' if ok else '未通过'}：{_flat_line(body)[:160]}"
            )
        lines = "；".join(" ".join(item.split()) for item in bits if item)
        return lines[:600]

    def _revert_command(self) -> str:
        """五件的第 ⑤ 件：一条**可直接执行**的整轮回退命令（没有就空串）。

        **按 `run_id` 给，不检查「现在还有没有待退的东西」**：收尾报告是在
        落盘**之前**拼的（落盘在 `settle` 里），那一刻 `pending` 还是满的；
        而报告落到人手上时改动已经写进工作区了。给一条「现在看起来没东西可退」
        的命令，等于把「有回退」这件事说反——`spool revert --run <id>` 在
        真的没有记录时只会报一句「没有这条运行记录」，那比不给命令安全得多
        （用户至少知道该敲什么）。
        """
        if self.pending is None:
            return ""
        run_id = str(getattr(self.pending, "run_id", "") or "")
        if not run_id:
            return ""
        try:
            has_work = bool(len(self.pending)) or bool(
                getattr(self.pending, "landed", lambda: [])()
            )
        except TypeError:  # pragma: no cover - pending 不是清单时按无回退处理
            return ""
        if not has_work:
            return ""
        return f"spool revert --run {run_id}"

    def _escalation_note(self) -> str:
        """收尾报告里那一段（没有升级就返回空串）。"""
        return render_decisions(self.escalation_gate)

    def _completion_blocked(self, changed: Sequence[str]) -> str:
        """收尾这一刻判「许不许宣称完成」（§3.1）。

        返回空串表示放行；非空表示那句话就是**降级后的结论**（逐条带
        `file:line`）。**这一步不动落盘、不回滚任何东西**：成果已经在
        工作区里，这里拦的只是那句「完成」（§2.5）。
        """
        self._review_fixable = False
        if not changed:
            return ""
        if self.pending is None:
            # **没有待确认清单就没有回退能力可言**：这一层挂在「落盘的每一处都
            # 配一份 Baseline」之上（§2.5 第 2 条），而 Baseline 由 `PendingChanges`
            # 攒。清单都没接上时，能诚实说的只有「这一轮没有可退的东西」，
            # 不能凭空判一条 T11——那会把每一次没有 pending 的运行都判成未完成。
            return ""
        files = list(dict.fromkeys(changed))
        lines = 0
        # 这些路径**将要**变成什么（待确认清单里的新正文）：T1 的符号快照要拿
        # 它当「之后」——收尾判这一刻改动还没落盘，读磁盘只会拿到改动前那一份。
        written: dict[str, str] = {}
        for path in files:
            change = None
            if self.pending is not None:
                change = next(
                    (item for item in self.pending.items() if item.path == path), None
                )
            if change is not None:
                lines += len(str(change.new_text).splitlines())
                written[path] = str(change.new_text)
            else:
                lines += self._disk_lines(path)
        regression = (
            self._final_verify is not None and self._final_verify[0] is False
        )
        # ---- T11/T13 的前置条件：**这一轮给不给得出回退**（§2.5 第 2 条）----
        #
        # **判据是机械的一件事实**：每个待落盘的路径，改动前的内容读不读得出来
        # （`pending.baseline()` 给不出条目就是这个意思，`apply()` 里那几处会被
        # `refused()` 挡下、不落盘）。判的**不是**「磁盘上有没有这个文件」——
        # 新建文件当然在磁盘上找不到，而它的回退就是「删回不存在」，那是
        # 有回退（§2.5 第 2 条明说了这一条）。
        #
        # 口径写错过一次（实测抓出来的）：一开始拿「磁盘上有没有这个文件」
        # 当判据，于是**每一次改既有文件**都命中 T11、收尾一律判「未完成」——
        # 升级率直接翻倍，而它拦的东西根本退得回去（`spool revert` 给的正是
        # 改动前的原文）。
        revert_command = self._revert_command()
        baseline_ok = True
        if self.pending is not None and len(self.pending):
            try:
                refused = self.pending.unbaselined()
            except (OSError, ValueError, AttributeError):
                refused = None
            baseline_ok = not refused
        elif not revert_command:
            # 没有待确认改动、也没有已经落成的改动：这一轮没有可退的产出，
            # 也就没有「给不出回退」这回事，不打扰（§3.2.1 的默认路径）。
            baseline_ok = True
        requests = self.escalation_gate.completion_triggers(
            self.audit_log.findings,
            files=files,
            lines=lines,
            regression=regression,
            # T8 的判据由 L2 给出（P2 接上的就是这一半）：**对照运行输出逐字
            # 不一致 / 优化类任务行为变了或没变快 / CLI 接口被删改签名**。
            # 原先这里借的是「另有高危发现」那道门槛（那时还没有 L2），
            # 而那条门槛的理由正是「改测试→改代码」类任务开头必然先红一次：
            # 现在由 L2 直接回答「行为变了没有」。
            #
            # **两道判据取或**（不是替换）：门槛原来覆盖的那些形状（生成物
            # 不存在、越界写这类高危发现）与 L2 回答的是不同问题，撤掉它等于
            # 用一条新判据换掉一条旧覆盖——那不是接线，是丢覆盖。
            blocking_failure=self._behavior_regression()
            or bool(self.audit_log.high()),
            has_baseline=baseline_ok and bool(revert_command),
            revert_command=revert_command,
            written=written,
            # T7 的充分条件那一半（§3.2.4）：**这一批已经被验证过**时，「改了
            # 6 个文件」本身不再是打扰人的理由——规模只是必要条件。
            verified=bool(
                self._final_verify is not None and self._final_verify[0] is True
            ),
            # T9（L3 高严重度发现未被处理）：**只降结论**——成果照落盘、可回退、
            # 报告逐条列出（§3.2.2 处置表把 T9 归在「纯语义」那一档）。
            semantic=self.semantic_high_findings(),
        )
        if not requests:
            # 没有触发条件（这一轮清清白白）：清掉上一轮留下的拦下判词，
            # 否则收尾报告会搬一句过期的「未完成」出来。
            self._review_blocked = False
            self._review_block_note = ""
            self._review_fixable = False
            return ""
        # **同一件事每一轮都重算一遍**（不是只算第一次）：`_escalate` 对已经
        # 请示过的那些只做记录、不再打扰（§3.2.4 的打扰预算），而这一段判词
        # 必须**每一轮都拿得到**——自动修那一关与收尾报告都要它。写成「第一次
        # 才算」的话，第二轮就拿到空串，于是自动修一轮都不给、`_review_fixable`
        # 也跟着判错（实测抓出来的）。
        for request in requests:
            self._escalate(request)
        high = self.audit_log.high()
        # 「自动修有限轮」只在**有东西可修**时才修：高严重度发现逐条带
        # `file:line`，模型照着改得动；而纯规模条件（T7 这类）里模型改什么都
        # 不会让那条线消失，再问一次只会白烧一轮。两种情形都照 §3.1 报
        # 「未完成 + 逐条 file:line」，区别只是要不要先给它一次补救机会。
        self._review_fixable = bool(high)
        listed = "\n".join(
            f"  - {item.where or '（未给位置）'}：{item.summary}" for item in high
        ) or "  - （没有高严重度发现，升级来自规模/回归条件）"
        triggers = "、".join(sorted({item.trigger for item in requests}))
        return (
            "⚠ 未完成（被审查拦下）："
            f"命中触发条件 {triggers}。这一轮的改动**已经落在工作区里**"
            "（拦的是「完成声明」与危险动作，不是成果），要退用下面的回退命令。\n"
            "逐条发现：\n"
            f"{listed}\n"
            "被拦下、等批复的动作见「风险分级放行」那一段。"
        )

    def _boundary_report(self) -> str:
        """收尾报告里那一段高危发现（没有就返回空串）。

        「**必须可见**」这条要求的落点：发现不能只躺在 `audit_log` 里，
        收尾那一份人真的会读的文本里要有它。
        """
        lines = self.audit_log.high_lines()
        if not lines:
            return ""
        return "\n本次运行的自动审查发现：\n" + "\n".join(f"- {line}" for line in lines)

    # ---- L1 机械一致性（P0：**只报不阻断**）--------------------------------

    def _mechanical_review(
        self, changed: Sequence[str], *, interim: bool = False
    ) -> None:
        """改完之后把机械一致性判一遍：残留引用 / 删除引用 / 引用闭合 / 测试改弱。

        **P0 的纪律**：这一层只把发现送进事件流、trace 与收尾报告，
        **不碰 `finished`、不碰落盘判定、不新增请示**——阻断语义是 P1。

        `interim=True` 是「某一步刚改完」，此时**不判引用闭合**：多文件改动
        天生多步，先写 A（它 import 了同一步里还没落盘的 B）是正常中间态，
        拿它去报「模块不存在」就是误报。闭合只在收尾那一刻判——那时树是终态。

        三条可见性出口里，残留那一条同时还有第四条：`_lingering_text` 会把它
        回灌到模型面前（模型正要在结论里写「已统一」）。其余发现不进模型上下文
        ——每改一个文件就往对话里塞一段提醒，会挤掉真正要它读的东西。
        """
        for finding in self._mechanical_findings(changed, interim=interim):
            self._emit_review_finding(finding)

    def _mechanical_findings(
        self, changed: Sequence[str], *, interim: bool = False
    ) -> list[Finding]:
        """机械判据算出来的那批发现（**只算，不落账本、不发事件**）。

        与 `_mechanical_review` 分开是因为收尾要**重判**（见
        `_rejudge_mechanical`）：重判必须能拿到一份干净的判词，而不是在
        「已经发过的那些」上面继续叠。
        """
        if self.pending is None:
            return []
        root = self.config.project_root
        findings: list[Finding] = []
        # ① 残留引用（含**被改的文件自己**、含**被删掉的文件**）。
        for change in self.pending.items():
            for hit in lingering_hits(root, self.pending, change):
                findings.append(_hit_finding(hit))
        # ② 引用闭合：改过的 .py 导入了本仓并不存在的本地模块（只判终态）。
        if not interim:
            for hit in broken_local_imports(root, self.pending, list(changed)):
                findings.append(_hit_finding(hit))
        # ③ 测试被改弱（删用例 / skip·xfail / 放宽断言 / conftest / pytest 配置 /
        #    测试数据）——只报不阻断。
        for change in self.pending.items():
            findings.extend(
                tests_weakened(
                    path=change.path,
                    before=change.old_text,
                    after=change.new_text,
                    deleted=change.deleted,
                )
            )
        return findings

    def _mechanical_scope(self, changed: Sequence[str]) -> list[str]:
        """重判要覆盖的路径：这一轮的改动 + 本次运行**所有**待落盘改动。

        只传「这一轮改过的」会漏掉前几轮的判词（引用闭合那一半按路径判）：
        重判时把它撤掉了，却没有按同一批路径重算一遍，那条真发现就凭空消失。
        """
        paths = list(changed)
        if self.pending is not None:
            paths += [item.path for item in self.pending.items()]
        return list(dict.fromkeys(paths))

    def _rejudge_mechanical(self, changed: Sequence[str]) -> None:
        """收尾那一刻**重判**机械发现：旧判词作废，按终态内容重新登记。

        为什么必须重判（这是实测抓出来的）：判词是**就某一版内容**下的结论。
        中途那一版上有残留引用，模型随后把文件改好了；而账本是只累加不清的，
        于是收尾报告与 T12（判据原话是「自动修 N 轮后**仍在**」）拿着一条
        **已经不成立**的判词去拦完成声明——§4 说得很清楚，L1 是机械判据，
        它有误报就是判据写错了。

        已经发过的那条 note **不重发**：探针按 note 数发现条数，重发会把同一个
        缺陷数成两条（`stale_references` 这类必填读数会凭空翻倍）。
        """
        findings = self._mechanical_findings(
            self._mechanical_scope(changed), interim=False
        )
        self.audit_log.forget_kind(MECHANICAL_KINDS)
        for finding in findings:
            signature = (finding.kind, finding.path, finding.line, finding.summary)
            if signature in self._reviewed_notes:
                # 仍然成立：账本要有它（T12 与收尾报告都读账本），事件不再发一次。
                self.audit_log.add(finding)
                continue
            self._emit_review_finding(finding)

    # ---- L2 行为回归（P2，§5 L2）------------------------------------------

    def _behavior_key_for(self, changed: Sequence[str]) -> str:
        """这一批改动的缓存键：**路径 + 内容指纹**。

        只按路径集合会命中「中途那一版」（同一个 bug 形状在最终验证的缓存上
        已经踩过一次，见 `_with_final_state` 那段注释）：对照运行的读数是对
        **某一份内容**下的结论，内容变了就必须重跑。
        """
        parts: list[str] = []
        for path in sorted(set(changed)):
            change = next(
                (item for item in (self.pending.items() if self.pending else [])
                 if item.path == path),
                None,
            )
            body = str(change.new_text) if change is not None else self._disk_text(path)
            parts.append(f"{path}|{_content_fingerprint(body)}")
        return "\n".join(parts)

    def _behavior_review(self, changed: Sequence[str]) -> BehaviorReport:
        """跑一遍 L2（对照运行 / 接口快照 / 性能不倒退），把发现送进账本与事件流。

        三条出口与 L1 完全一样（账本 / 事件流 / 收尾报告），**处置也一样**：
        高严重度只拦「完成声明」与危险动作，**不拦成果**（§2.5）。成果照落盘、
        可回退、报告逐条列出；删死代码、合并重复实现、删已被替代的文件都是
        正常编辑，这一层判的是「同一条命令、同一份输入，输出是否一字未变」。

        **读数与发现走两个出口**（问题③）：发现照旧进账本/事件流/报告；读数
        （判据原话、命令、逐字比对、耗时、「查不了」的理由）只进事件流与
        trace——它是过程材料，结论报告里不出现系统账目原文。
        """
        key = self._behavior_key_for(changed)
        if self._behavior_report is not None and key == self._behavior_key:
            return self._behavior_report
        # 旧判词先撤掉：判词是对某一版内容的结论，这一版可能已经改好了（与
        # L1 的重判同一个理由）。
        self.audit_log.forget_kind(BEHAVIOR_KINDS)
        pending_items = list(self.pending.items()) if self.pending is not None else []
        timeout = float(self.config.limit("behavior_entry_timeout"))
        # **这一层不许把运行弄崩**：它挂在收尾出口上，一个判据里的意外异常
        # （复制失败、解释器路径怪、README 编码坏）会让一次正常收尾变成崩溃。
        # 出不了结论时如实报「查不了」——那是一个读数，不是通过（§5 L2 表）。
        try:
            report = review_behavior(
                self.config.project_root,
                pending_items,
                goal=self._goal_text,
                interface_changes=self.escalation_gate.changed_public_symbols(
                    self._written_view(changed)
                ),
                timeout=timeout,
            )
            # L2.3 的另一半：CLI `--help` 快照（公开符号看不出子命令/参数被改名）。
            cli_files = cli_help_candidates(self.config.project_root, changed)
            if cli_files:
                same, findings = compare_cli_help(
                    self.config.project_root,
                    pending_items,
                    cli_files,
                    timeout=timeout,
                )
                report.cli_checked = same
                report.cli_files = tuple(cli_files)
                report.findings.extend(findings)
        except Exception as exc:  # noqa: BLE001 - 判据出不了结论不是运行失败
            report = BehaviorReport(
                cannot_check=(
                    f"行为回归这一步没能得出结果（{type(exc).__name__}: {exc}）"
                )
            )
        for finding in report.findings:
            self._emit_review_finding(finding)
        self._behavior_report = report
        self._behavior_key = key
        # **读数进过程区，不进结论报告**（问题③的最后一处残余）：
        # `_behavior_readings` 给的是判据原话、实际跑的命令、逐字比对的结果与
        # 「查不了」的理由——它们是排查用的材料。它们在结论报告里出现时，
        # §2.5 的反向条款会跟着一起出现，于是"什么都没拦下"的运行里也读到一段
        # 无关样板（risky-action 那一轮实测的形状）。报告那一侧只留用户语言的
        # 一句，见 `_behavior_report_section`。
        readings = self._behavior_readings(report)
        if self._behavior_material(report, changed):
            self._emit(
                "note",
                {"text": readings, "ok": True, "kind": BEHAVIOR_READINGS_KIND},
            )
        self._log(f"自动审查 L2：{readings}")
        return report

    def _written_view(self, changed: Sequence[str]) -> dict[str, str]:
        """这些路径**将要**变成什么（收尾判这一刻改动还没落盘）。"""
        out: dict[str, str] = {}
        for path in changed:
            change = next(
                (item for item in (self.pending.items() if self.pending else [])
                 if item.path == path),
                None,
            )
            if change is not None:
                out[path] = str(change.new_text)
        return out

    def behavior_findings(self) -> list[Finding]:
        """本次运行 L2 报出的**真**发现（不含「查不了」那条读数）。"""
        kinds = (
            BEHAVIOR_CHANGED_KIND,
            CLI_SURFACE_CHANGED_KIND,
            PERFORMANCE_REGRESSED_KIND,
        )
        return [item for item in self.audit_log.findings if item.kind in kinds]

    def _behavior_regression(self) -> bool:
        """L2 判出**该拦完成声明**的行为回归没有（T8 的判据）。

        判据是机械的一条：账本里有没有 L2 的**高严重度**发现。

        - 「查不了」（`behavior_not_checked`）**不算**回归——把它算成回归等于
          「测不出来就判失败」，那是另一种静默放行。
        - 提示档（`INFO`）也不算：非优化类任务上的行为变化、公开符号被删改
          签名都是**读数**——前者是任务本来就要求的改动，后者由 T1 那条既有
          通道升级（同一批证据，不重复拦一次）。把它们算成回归会去拦一次
          正确的完成声明（探针 `rename-cross-file` 上实测过：改名任务被 L2
          再拦一次，`final_ok` 从「本来就红」变成「多红一条」）。
        """
        return any(item.severity == HIGH for item in self.behavior_findings())

    def _behavior_material(
        self, report: BehaviorReport, changed: Sequence[str] = ()
    ) -> bool:
        """这一轮的 L2 有没有**说得出话**的材料（决定它是否出现）。

        两条来源：报告里真有读数（跑成了对照 / 有发现 / 接口动了 / 优化类任务），
        或者这一轮改的产出**有行为面**（`.py` 这类；问题⑥：只改了一个 `.txt`
        的运行不需要被告知「行为回归查不了」）。

        判据与整改前逐字一致——变的只是材料出现的地方：读数去事件流与 trace，
        用户那一句留在结论报告（见 `_behavior_report_section`）。
        """
        return bool(
            report.findings
            or report.checked
            or report.interface_changes
            or report.cli_checked is not None
            or report.optimization
            or _has_behavior_surface(changed)
        )

    def _behavior_readings(self, report: BehaviorReport) -> str:
        """L2 的完整读数：**过程材料**，只进事件流与 trace。

        判据原话、实际跑的命令、逐字比对的结果、耗时、「查不了」的理由都在这儿
        ——排查看的就是这一份。它**不进结论报告**：那是用户读的那一份，里面
        出现内部读数时会连带把 §2.5 的反向条款拖进去（问题③）。
        """
        lines = [f"- {report.render()}"]
        if report.cli_checked is not None:
            verdict = "一致" if report.cli_checked else "**不一致**"
            files = "、".join(report.cli_files) or "（没有 CLI 入口）"
            lines.append(f"- L2.3 命令行接口快照：`--help` 前后{verdict}（{files}）")
        if report.interface_changes:
            listed = "、".join(report.interface_changes[:5])
            lines.append(f"- L2.3 公开符号快照：被删或改签名 —— {listed}")
        if report.optimization:
            speed = {
                True: "有实测改善",
                False: "**没有实测改善或性能倒退**",
                None: "没测出快慢（耗时太短，噪声比差值大）",
            }[report.faster]
            lines.append(
                f"- L2.4 优化类任务：行为"
                f"{'未变' if report.same else '**已变**'}、{speed}"
                "（行为变了就算更快也不通过）"
            )
        if report.cannot_check and report.checked:
            # 「查不了」的理由在 `render()` 里已经带过一次；只有**比对跑成了**
            # 但前置步骤（如造输入）没做成时才需要再补一句。
            lines.append(f"- {CANNOT_CHECK}{report.cannot_check}")
        body = "\n".join(lines)
        return f"\n本次运行的 L2 行为回归读数（过程材料，不进结论报告）：\n{body}"

    def _behavior_report_section(self, changed: Sequence[str] = ()) -> str:
        """收尾报告里的 L2 那一段：**只放用户需要的那一句**（问题③）。

        **读数不进这一份文本**：判据原话、实际跑的命令、逐字比对的结果、耗时、
        「查不了」的理由都是过程材料，它们在 `_behavior_review` 里进事件流与
        trace（`BEHAVIOR_READINGS_KIND` 那条 note）——用来跟踪 agent 的思路与
        排查问题。报告里出现内部读数时，§2.5 的反向条款会跟着一起出现，于是
        "什么都没拦下"的那一轮也读到一段无关样板（risky-action 那一轮实测的
        形状：L2 报了「输出逐字不一致」，报告里就多出那句反向条款）。

        §2.5 的反向条款**只在真的拦下东西时**出现：它是给「被审查拦下」那句
        判词配的（`_review_block_note`）。没拦下时它答的是一个没人问的问题。

        **它按需出现**（问题③⑥）：既没有 L2 材料、改的又是 `.txt` 这类没有
        行为面的产出时，整段一个字都不出。
        """
        report = self._behavior_report
        if report is None:
            return ""
        if not self._behavior_material(report, changed):
            return ""
        lines: list[str] = []
        if report.changed:
            # 行为变了但**没拦**：一句用户语言，不带内部读数、不带反向条款
            # （读数为 `L2.2 对照运行：…输出**逐字不一致**`，它在事件流里）。
            lines.append("- 这次改动会改变程序原有行为。")
        if self._review_blocked:
            lines.append(
                "（拦的是「完成声明」与危险动作，**不是成果**：改动照落盘、可回退、"
                "报告逐条列出；删死代码、合并重复实现、删已被替代的文件都是正常编辑。）"
            )
        if not lines:
            return ""
        return "\n本次运行的 L2 行为回归：\n" + "\n".join(lines)

    # ---- L3 语义审查（P2 后半，§5 L3）--------------------------------------
    #
    # 三条纪律落在这一组方法里（判据本体在 `agent/review_semantic.py`，这里只
    # 做接线与出口）：
    #   ① **独立上下文**：送审材料由 `_semantic_materials` 拼，里面**没有**模型
    #      那一段收尾结论——「不把自我陈述喂给审查者」因此是结构上成立的，
    #      不靠调用方记得少传一个参数（§9.2）。
    #   ② **结构化 + 事实核对**：解析、`ReviewFacts` 核对、降级都在那一层。
    #   ③ **阻断语义与 L1/L2 一致**：发现进同一个账本、走同一个出口
    #      （`_emit_review_finding` → `audit_log` → T9/T12 → 「不许宣称完成」），
    #      而**成果照落盘**（§2.5）。

    def _semantic_key_for(self, changed: Sequence[str]) -> str:
        """这一批改动的缓存键：与 L2 同一个口径（路径 + 内容指纹）。

        为什么不能只按路径集合：一次模型审查的判词是对**某一版内容**下的结论，
        内容变了就必须重跑，否则「上一版说没问题」会被当成「这一版也没问题」。
        """
        return self._behavior_key_for(changed)

    def _semantic_evidence(self) -> list[str]:
        """跑过什么、结果如何（**原话**，不是总结）。

        判据故意只认两样机械事实：最终验证那条结论、以及账本里 L1/L2 的发现。
        主循环的 trace 与模型那一段收尾结论**都不进**——前者是实现者的推理，
        后者是它的自我陈述。
        """
        out: list[str] = []
        if self._final_verify is not None:
            ok, body = self._final_verify
            out.append(
                f"最终验证：{'通过' if ok else '未通过'}——{_flat_line(body)[:400]}"
            )
        else:
            out.append(f"{CANNOT_CHECK}收尾这一刻没有拿到验证结论（本次运行没挂验证器）")
        for finding in self.audit_log.high()[:8]:
            out.append(f"发现（{finding.kind}）：{finding.where or '（未给位置）'} {finding.summary}")
        return out

    def _semantic_materials(self, changed: Sequence[str]) -> ReviewMaterials:
        """拼送审材料。**刻意不含模型那一段收尾结论**（独立上下文的边界）。"""
        changes: list[ChangeFacts] = []
        diff_parts: list[str] = []
        for path in list(dict.fromkeys(changed)):
            item = next(
                (
                    one
                    for one in (self.pending.items() if self.pending is not None else [])
                    if one.path == path
                ),
                None,
            )
            if item is None:
                continue
            old_lines = str(item.old_text).splitlines()
            new_lines = str(item.new_text).splitlines()
            changes.append(
                ChangeFacts(
                    path=item.path,
                    edited=not item.is_new_file and not item.deleted,
                    created=item.is_new_file,
                    deleted=bool(item.deleted),
                    added=len(new_lines),
                    removed=len(old_lines),
                )
            )
            diff_parts.append(item.diff)
        behavior: list[str] = []
        report = self._behavior_report
        if report is not None:
            behavior.append(report.render())
            behavior += [item.render() for item in report.findings[:8]]
        # 回退能力（§2.5 第 2 条）：判据与收尾闸门同一份材料。读不出来时按
        # 「有」处理——那一边（`_completion_blocked`）会自己判一遍，这里只把
        # 事实告诉审查者，不该成为它拒答的理由。
        has_baseline = True
        if self.pending is not None and len(self.pending):
            try:
                has_baseline = not self.pending.unbaselined()
            except (OSError, ValueError, AttributeError):
                has_baseline = True
        return ReviewMaterials(
            goal=self._goal_text,
            changes=tuple(changes),
            diff="\n".join(diff_parts),
            mechanical=tuple(
                item.render()
                for item in self.audit_log.findings
                if item.kind in MECHANICAL_KINDS
            )[:8],
            behavior=tuple(behavior),
            checks_run=tuple(self._semantic_evidence()),
            revert_command=self._revert_command(),
            has_baseline=has_baseline,
            workspace=self._file_state_line(list(changed)) if changed else "",
        )

    def _semantic_reading(self, report: SemanticReport, decision: TriggerDecision | None) -> str:
        """L3 的**过程读数**（只进事件流与 trace，不进结论报告）。

        与 L2 的读数同一个口径：触发条件、跑没跑、被剔除几条、「查不了」的理由
        都是排查材料。结论报告里只留 `review_semantic.render_report` 那一段
        ——它只在真的拦下东西时才有内容。
        """
        lines = [f"- {report.render()}"]
        if decision is not None and decision.reasons:
            lines.append("- 命中的触发条件：" + "；".join(decision.reasons))
        if report.unverified:
            lines.append(f"- 被事实核对降级 {report.unverified} 条（已按记录改写）")
        if report.conflicts:
            parts = "、".join(
                f"{key} {value}" for key, value in sorted(report.conflicts.items())
            )
            lines.append(f"- 与记录冲突的断言分档：{parts}")
        if report.reasons:
            lines.append("- 审查者给的理由：" + "；".join(report.reasons[:3]))
        body = "\n".join(lines)
        return f"\n本次运行的 L3 语义审查读数（过程材料，不进结论报告）：\n{body}"

    def _semantic_review(
        self, changed: Sequence[str], *, prior_verified: bool | None = None
    ) -> SemanticReport | None:
        """跑一遍 L3（或因触发条件不满足而跳过），把发现送进账本与事件流。

        **一次运行里同一份内容只跑一遍**：收尾那条「沿用上次验证」的捷径与收尾
        本体都会走到这里，重跑只是再花一次模型调用、结论一模一样。

        `prior_verified` 是**上一版内容**的验证结论（`_with_final_state` 传的
        是 `_final_verify`，因为这一版的验证还没跑）。账本缺口的判据要它：
        拿 None 去算的话每一轮都会报「没跑」，L3 就变成「每步都跑」了。
        """
        if not changed or self.pending is None:
            return self._semantic_report
        key = self._semantic_key_for(changed)
        if self._semantic_report is not None and key == self._semantic_key:
            return self._semantic_report
        # 触发条件：五条里的四条在这里判（第 5 条走配置/调用方显式要求）。
        #
        # 「存在没有证据的完成声明」用的是**那个版本的验证结论**：账本按它算
        # 缺口，而不是按「此刻还没跑」——后者会让每一轮都命中 F4。
        decision = semantic_triggers(
            finding_count=len(self.audit_log.findings),
            regression=self._behavior_regression(),
            files=len(set(changed)),
            lines=sum(
                len(str(one.new_text).splitlines())
                for one in self.pending.items()
                if one.path in set(changed)
            ),
            evidence_gaps=self._ledger_gaps(list(changed), prior_verified),
            explicit=bool(getattr(self.config, "semantic_review", False)),
            max_files=self.config.limit("review_trigger_files"),
        )
        # 旧判词先撤掉：判词是对某一版内容的结论，这一版可能已经改好了
        # （与 L1/L2 的重判同一个理由）。
        self.audit_log.forget_kind(L3_KINDS)
        try:
            report = review_semantic(
                self._semantic_materials(changed),
                root=self.config.project_root,
                decision=decision,
                chat=self.gateway,
                max_tokens=int(self.config.limit("review_max_tokens")),
                max_findings=int(self.config.limit("review_max_findings")),
                # 事实核对那条「说某把工具不存在」的判据要**注册表里的名字**
                # （与督导那一套同一个来源：`_run_facts` 也是从注册表取的）。
                tools=frozenset(self.registry.names()),
            )
        except Exception as exc:  # noqa: BLE001 - 判据出不了结论不是运行失败
            report = SemanticReport(
                ran=False,
                trigger=decision.note,
                cannot_check=(
                    f"{CANNOT_CHECK}L3 这一步没能得出结果"
                    f"（{type(exc).__name__}: {exc}）"
                ),
            )
        # ---- 三条出口 ---------------------------------------------------------
        # ① 发现 → 账本 + 事件流（走 L1/L2 同一个出口：同一套严重度、同一套
        #    `file:line`、同一套去重；T9/T12 与收尾报告据此判）。
        for finding in semantic_findings(report):
            self._emit_review_finding(finding)
        if report.findings:
            # 结构化那一份单独发一条（**读的人是壳与探针，不是模型**）：
            # 每条发现的 file/line/severity/evidence 与「过没过事实核对」都在
            # 这里，探针不必去解析散文（§4、§6 字段表）。
            self._emit(
                "note",
                {
                    "text": "L3 语义审查发现（结构化）",
                    "ok": not report.blocked,
                    "kind": L3_DETAIL_KIND,
                    "findings": [item.to_payload() for item in report.findings],
                    "blocking": len(report.blocking),
                    "unverified": report.unverified,
                    "conflicts": dict(report.conflicts),
                },
            )
        # ② 读数 → 事件流与 trace（触发理由一份就够：它是读数，不是按版本变化的
        #    结论；每换一版内容再发一条只会把事件流灌满）。
        if not self._semantic_decision_sent or decision.run:
            self._semantic_decision_sent = True
            self._emit(
                "note",
                {
                    "text": self._semantic_reading(report, decision),
                    "ok": True,
                    "kind": L3_READINGS_KIND,
                    **semantic_reading_payload(report),
                },
            )
            self._log(f"自动审查 L3：{report.render()}")
        # ③ 「L3 拦下完成声明」→ 由 `_apply_completion_gate` 在真的拦下时发。
        if report.ran:
            self._semantic_runs += 1
        self._semantic_report = report
        self._semantic_key = key
        return report

    def semantic_high_findings(self) -> list[Finding]:
        """L3 报出的、**过了事实核对**的高严重度发现（T9 的输入）。"""
        return [
            item
            for item in self.audit_log.of_kind(SEMANTIC_FINDING_KIND)
            if item.severity == HIGH
        ]

    def _semantic_report_section(self) -> str:
        """收尾报告里的 L3 那一段（**只放结论与逐条 `file:line`**，不贴 diff）。

        三条口径与 L2 的报告段一致：

        - **读数不进报告**：触发条件原话、「查不了」的理由、被剔除几条都是过程
          材料，它们在事件流与 trace 里（`L3_READINGS_KIND` 那条 note）。
        - **被事实核对降级的那几条也要列出来**：静默丢掉等于把「审查者说错话」
          这件事藏起来（§4）。但标明「与记录不符、不据此拦完成声明」。
        - **§2.5 的反向条款只在真的拦下东西时出现**：它是给「被审查拦下」那句
          判词配的，不是每一轮都要念一遍的样板（问题③）。
        """
        report = self._semantic_report
        if report is None:
            return ""
        # **重复不上报**：真的拦下时，逐条发现已经在那句「⚠ 未完成（被审查拦下）」
        # 里列过一次（`_completion_blocked` 的 `listed`），这里再列一遍就是同一件
        # 事说两遍（那条纪律在 `_collect_report` 的注释里）。所以这一段只补
        # **被事实核对降级的那几条**——它们不在那句判词里，而静默丢掉等于把
        # 「审查者说错话」这件事藏起来（§4）。
        body = "" if self._review_blocked else render_semantic_report(report)
        downgraded = render_semantic_unverified(report)
        if not body and not downgraded:
            return ""
        parts = [body] if body else []
        if downgraded:
            parts.append(downgraded)
        return "\n" + "\n".join(parts)

    def _emit_review_finding(self, finding: Finding) -> None:
        """一条机械发现的三条出口：账本、事件流、trace。同一条只发一次。

        事件流的 `kind` 按**判据分类**给（`note_kind`）：探针要单独数
        `stale_references` / `tests_weakened`，不能靠读散文去认（设计稿 §6 的
        字段表与 §4「每条发现必须带 file:line 与机械证据」）。
        """
        signature = (finding.kind, finding.path, finding.line, finding.summary)
        if signature in self._reviewed_notes:
            return
        self._reviewed_notes.add(signature)
        self.audit_log.add(finding)
        self._emit(
            "note",
            {
                "text": finding.render(),
                "ok": finding.severity != HIGH,
                "kind": note_kind(finding.kind),
            },
        )
        self._log(f"自动审查：{finding.render()}")

    # ---- L4 最小证据账本（P0：**只在报告层降级**）--------------------------

    def _ledger_gaps(self, changed: Sequence[str], passed: bool | None) -> int:
        """只**算**证据缺口，不发事件、不改任何状态（供 L3 的触发条件判据用）。

        为什么要与 `_evidence_ledger` 分开：那一个是收尾出口，它算完会发一条
        `evidence_gaps` note 并把计数记进本次运行。L3 在收尾**之前**判「有没有
        没有证据的完成声明」时借用同一份材料，如果直接调那一个，就会在验证
        还没跑的那一刻先报一条假的「没跑」缺口——那是**凭空造出来的读数**，
        实测把 `tests/agent/test_evidence_ledger.py::test_验证通过时不降级`
        判红了（那条用例正是钉「验过了就不许报缺口」的）。
        """
        ledger = ReviewLedger()
        if not changed:
            return 0
        claim = f"这次运行的完成结论（{len(changed)} 处改动）"
        if self.verify is None:
            ledger.record(
                claim, "自动验证", CANNOT, "本次运行没有挂验证器，一次检查都没跑"
            )
        elif passed is None:
            ledger.record(
                claim, "最终验证", NOT_RUN, "收尾这一刻没有拿到验证结论"
            )
        else:
            ledger.record(
                claim,
                "最终验证",
                PASS if passed else FAIL,
                "",
                content_key=self._last_verify_key,
            )
        return len(ledger.gaps())

    def _evidence_ledger(
        self, changed: Sequence[str], passed: bool | None
    ) -> ReviewLedger:
        """这次运行的「完成」声明挂上了哪条检查。

        P0 只做一件事：**一条有结论的检查都没挂上时，那句完成在报告里降级为
        「未验证」**（`ReviewLedger.downgrade_note`）。它不改 `finished`、
        不改落盘判定、不新增请示流——那些是 P1。
        """
        ledger = ReviewLedger()
        if not changed:
            # 一处改动都没有时没有「完成声明」可挂：只读角色不该被这句话打扰。
            return ledger
        claim = f"这次运行的完成结论（{len(changed)} 处改动）"
        if self.verify is None:
            ledger.record(
                claim, "自动验证", CANNOT, "本次运行没有挂验证器，一次检查都没跑"
            )
        elif passed is None:
            ledger.record(
                claim, "最终验证", NOT_RUN, "收尾这一刻没有拿到验证结论"
            )
        else:
            ledger.record(
                claim,
                "最终验证",
                PASS if passed else FAIL,
                (self._final_verify[1] if self._final_verify else "")[:120],
                content_key=self._last_verify_key,
            )
        gaps = ledger.gaps()
        if gaps and not self._evidence_gaps:
            self._evidence_gaps = len(gaps)
            self._emit(
                "note",
                {
                    "text": ledger.downgrade_note(),
                    "ok": False,
                    "kind": EVIDENCE_GAPS_KIND,
                    "count": len(gaps),
                },
            )
            self._log("证据账本：" + "、".join(gaps) + " 没有证据 → 降级为未验证")
        return ledger

    def _log(self, line: str) -> None:
        """往本轮的 trace 上追加一行（给人和督导看的那一份）。

        与 `self._emit` 分开：emit 发给外部观察者（网页壳、`--events`），
        trace 是督导的证据。护栏的放行理由两边都要有，所以两个出口各写一次。
        """
        if self._trace is not None:
            self._trace.append(line)

    def _disk_text(self, path: str) -> str:
        """这个路径**磁盘上的**原文（读不到给空串）。

        与 `_text_of` 的区别是**不叠加待确认改动**：判「改动前是什么」必须用
        磁盘那一份，而 `read_file` 看到的、护栏比对的、验证器验的都是叠加后的
        视图。两件事各有各的用处，别混。
        """
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self.config.project_root / path
            return target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

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

    def _pending_state_key(self) -> str:
        """待确认清单的指纹：**「已提出、还没落盘的改动」这一集合**长什么样。

        它是「世界变了没有」的廉价且准确的代理，重复调用护栏用它：清单变了，
        重发一次可能拿到新结果；清单一字未动，同参数的第二次拿到的还是同一份。
        取不到（没有 pending 这一层）就返回空串——那等于「判不出」，由调用方
        退回「提醒」路径。

        为什么不用「改动落成的次数」：那一条**分不出**「同一个调用的第一次与
        第二次之间什么都没发生」——暂存本身就会把它推上去，于是每一个重复都看
        起来「有变化」（实测 merge-html r0 的第二次 write_file 就是这么被放行的）。

        每个条目的指纹取「路径 + 新正文的字符数 + 行数」：重复护栏要判的是
        「这一集合动过没有」，不是「一个字有没有改」，而哈希整份正文在大文件上
        要按 MB 算，每次重复调用都算一遍不值。精确到字节的是**调用对象**那一侧
        （`_call_state`），那里本来就有整份内容可哈希。
        """
        if self.pending is None:
            return ""
        try:
            items = sorted(
                f"{str(getattr(item, 'path', ''))}:"
                f"{len(str(getattr(item, 'new_text', '') or ''))}:"
                f"{len(str(getattr(item, 'new_text', '') or '').splitlines())}"
                for item in self.pending.items()
            )
        except Exception:  # pragma: no cover - pending 自己坏了不该毁掉护栏
            return ""
        return hashlib.sha1("|".join(items).encode("utf-8")).hexdigest()[:12]

    def _call_state(self, call: ToolCall) -> str:
        """**这次调用看的那个对象**现在长什么样；拿不到就返回空串。

        两种调用两种对象，这不是特例，而是「它到底在问什么」：
        - 读类（`read_file` / `find_symbol` …）：它问的是盘上/暂存区那一版内容，
          所以指纹取 `_text_of(path)`；
        - 写类（`write_file` / `replace_text` …）：它问的是**自己要交出去的那份
          正文**，所以指纹取参数里的 `content`。取盘上内容会把「暂存之后盘上还
          是旧的」读成「变了」，于是同一个暂存再发一遍永远被放行。
        """
        if call.name in WRITE_TOOLS:
            body = call.arguments.get("content")
            if not isinstance(body, str):
                return ""
            return _content_fingerprint(body)
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            return ""
        text = self._text_of(path)
        return "" if text is None else _content_fingerprint(text)

    def _guard_repeat_decision(
        self, signature: str, call: ToolCall
    ) -> tuple[str, str]:
        """把一次「重复调用」机械地归成三类之一，返回（归类, 理由）。

        这是整条护栏**唯一**的判据点：`_invoke_guarded` 的第一次重复与第 N 次
        重复、以及 `_refresh_reason`（它只是包一层放行上限），走的都是这里——
        两处各判一套的话，「第一次提醒、第三次硬拦」就会开始互相打架。

        判据只看能证伪的事实，不看它看起来像不像在打转：

        1. **上一次结果还在不在上下文里**（`_seen_at_revision`）。护栏在软/硬
           触发时会砍历史（`_context_revision` 在那两处 +1），砍掉之后那个签名
           虽然还在 `recent` 窗口里算「重复」，内容却不在模型手上了。这一条
           不成立就必须放行——硬拦的后果是模型手上只剩一份被拦的输出，
           什么都读不到，也走不出去。
        2. **这中间有没有可观察的变化**，两条独立证据：
           - 待确认清单变过（`_pending_state_key`）：那种「已提出、还没落盘」
             的改动也算数——`run_command` 会在试跑副本上带着它们跑，所以中间
             改过文件时重跑测试是有理由的（fix-bug 现场：先红后绿正是这条）；
           - 这次调用看的那个对象变了（`_call_state`）：内容指纹不等，旧结果
             描述的是旧内容，行号也可能已经漂了。
        3. **能不能判**：签名从没执行过、或这次调用根本没有可比较的对象
           （没有 `content`、也没有可读的 `path`）→ 判不出。

        剩下的情况（执行过 + 参数逐字相同 + 结果还在 + 两条证据都没变）就是
        **无用重复**：再跑一遍拿到的必然还是同一份结果，而它要付一次完整的
        模型生成与一次工具执行。这一类在**第一次重复**就要硬拦。
        """
        mark = self._seen_at_revision.get(signature)
        if mark is not None and mark < self._context_revision:
            return (
                REPEAT_REFRESH,
                "上一次的结果已经被上下文压缩丢弃（它不在你手上了）",
            )
        if signature not in self._observed_state:
            # 它从没真的执行过（比如上一次这一条被别的闸拦下了）：没有「上一次
            # 的结果」可以复用，谈不上重复。
            return REPEAT_UNDECIDED, "这个签名没有执行过的记录，判不出能不能复用上一次的结果"
        observed_state, observed_pending = self._observed_state[signature]
        if self._pending_state_key() != observed_pending:
            return (
                REPEAT_REFRESH,
                "这中间有改动真的落成了（待确认清单与上次不同），"
                "重发一次可能拿到不同的结果",
            )
        current = self._call_state(call)
        if not current or not observed_state:
            # 拿不到可比的对象（`run_command` 这类既没有正文、也没有可读路径的
            # 调用）：**如实记成判不出**，不假装判过。注意「当时拿不到」也算——
            # 两个空指纹互相「相等」，那种相等什么也证明不了。
            return (
                REPEAT_UNDECIDED,
                f"这个调用（{call.name}）没有可比较的内容对象"
                "（既不带正文、也不带可读路径），护栏判不出这次重复有没有意义",
            )
        if current == observed_state:
            path = call.arguments.get("path")
            what = (
                f"{path} 的内容"
                if isinstance(path, str) and path
                else "这次要交出去的正文"
            )
            return (
                REPEAT_USELESS,
                f"{what}自上次调用以来一字未变（指纹 {current}），"
                "上一次的结果仍在你的上下文里，待确认清单也没有动过",
            )
        return (
            REPEAT_REFRESH,
            "这次要看的对象与上次执行时不一样了，旧结果描述的是旧内容",
        )

    def _refresh_reason(self, signature: str, call: ToolCall) -> str:
        """重复调用该不该放行。放行返回**理由**，该拦返回空串。

        判据全部落在 `_guard_repeat_decision` 里（一处判据、一个口径）：
        只有它判成「有理由的重取」才返回理由，判成「无用重复」或「判不出」
        都返回空串——前者的处置是硬拦，后者的处置是保留提醒路径。
        """
        if self._refreshes_used >= _REFRESH_LIMIT:
            return ""
        decision, reason = self._guard_repeat_decision(signature, call)
        return reason if decision == REPEAT_REFRESH else ""

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
        """记下这次调用当时的**两条指纹**，供下一次重复调用判「该不该拦」。

        - `_call_state(call)`：这次调用看的那个对象长什么样（读类看盘上/暂存区
          内容，写类看它自己要交出去的正文）。
        - `_pending_state_key()`：当时的待确认清单长什么样。

        两条都只在这次调用**真的执行了**（`result.ok`）时才记：没执行的调用没有
        「上一次的结果」可复用。两条都取不到（`run_command` 这类没有内容的调用）
        时记空串——下一次进来会如实归成「判不出」，而不是假装判过。

        以前这里只认 `read_file` 的文件指纹，于是写类调用的重复永远拿不到可比
        对象，全落到「判不出」。现在两种调用各看各的对象，判据本身没变。
        """
        if not result.ok:
            return
        signature = call_signature(call)
        self._observed_state[signature] = (
            self._call_state(call),
            self._pending_state_key(),
        )

    def _forget_read(self, path: str) -> None:
        """这个文件被改过：清掉它的「读于哪个上下文版本」。

        留着它，重复调用的第一条判据（上一次的结果还在不在上下文里）会拿到一个
        描述旧文件的时间戳——文件已经变了，那份旧结果不该再被当成「还在手上」。
        `write_file` 提交/落盘那条路会走到这里。

        调用对象的指纹（`_observed_state`）**不清**：它记的是一个真实发生过的
        事实（这个签名确实在某一次执行时看到过那一版内容）。下一轮判据改用
        `_call_state(call)` 重新算一遍当前值，与它比就足够——清掉反而会让同一个
        调用在文件被改之后退化成「这个签名没有执行过的记录」，把「有理由的重取」
        错记成「判不出」。
        """
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
        """有改动真的落成了，就清空失败表：文件变了，旧参数可能重新有意义。

        「组合停用」的计数一起清：停用是**临时**的（用户的要求就是「暂时禁用」），
        解除条件与失败表同一个事实——盘上的东西变过了。
        """
        self._failed_calls.clear()
        self._failed_output.clear()
        self._blocked_combos.clear()
        self._reroute_evidence = ""

    def _refuse(
        self,
        call: ToolCall,
        signature: str,
        message: str,
        remedies: Sequence[str] = (),
    ) -> ToolResult:
        """一次「没有执行」的拦截：记账 → 到阈值就换成**停用 + 替代清单**。

        为什么必须记账：护栏原来只说不许，不数次数——同一条调用被拦 1 次还是
        3 次，在护栏眼里是同一件事，于是它每次都给同一句「换一种做法」，
        而模型对同一句话免疫（实测同参连拦 3 次，它的意图每次都写着「已经改好
        了」）。数到阈值之后，这个组合**停用**，话术也换成清单式的
        （`_combo_disabled_message`），并把这条轨迹留给督导。

        阈值 `reroute_block_at` 是可覆盖的标定值：默认 1 = 第一次被拦之后，
        这个组合的第二次发起就直接拒（不再陪着它复读一轮）。
        """
        count = self._blocked_combos.get(signature, 0) + 1
        self._blocked_combos[signature] = count
        block_at = int(self.config.limit("reroute_block_at"))
        if block_at <= 0 or count <= block_at:
            return ToolResult(ok=False, content=message)
        text = _combo_disabled_message(call, count, remedies, message)
        evidence = (
            f"同一个（工具 {call.name} + 同一组参数）的组合已经被硬拦 {count} 次，"
            "该组合已停用；已经给过的替代方案："
            + "；".join(remedies or ("换参数 / 换工具 / 先看别的 / 直接给结论",))
        )
        self._reroute_evidence = evidence
        self._log(f"组合停用：{call.name}（第 {count} 次被拦）——{evidence}")
        self._emit(
            "note",
            {
                "text": f"组合停用：{call.name}（第 {count} 次被拦）",
                "ok": False,
                "kind": COMBO_DISABLED_KIND,
                "tool": call.name,
                "count": count,
                "limit": block_at,
                "signature": signature,
            },
        )
        return ToolResult(ok=False, content=text)


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
        # ---- T10 的原料：这次验证里有哪些「查不了」 --------------------------
        #
        # 判据是**机械的**：结论里出现与 `tools/verify.py` 同一个字面标记
        # （`CANNOT_CHECK`，这里按同一个词认，不另造一套）。记下来之后，
        # T10 只在「同一批改动另有 T1–T7 命中」时才成立——不是每一句
        # 「查不了」都升级（那样每条 HTML 运行都会升级，见 `completion_triggers`）。
        for line in result.content.splitlines():
            if CANNOT_CHECK in line:
                self.escalation_gate.note_cannot_check(line)
        if found.findings:
            # 发现也要进 trace：调试时「这一步判出了什么」是唯一能看到的信号，
            # 而高危那条（迎合测试）本来就该在人读的轨迹里留一行。
            self.audit_trace(found)
        if result.ok:
            # **没有验证器的那一档不许说成「跑了一遍测试：通过」**（问题⑤）：
            # 那句回话是模型判断「我这个地方验过了」的唯一依据，而 `.txt` 这类
            # 产出根本没有判据可跑。如实说「没有校验器（不是失败）」——它既不
            # 挫伤模型，也不给它一个不成立的通过。
            #
            # 末尾那半句（`_MODEL_ONLY_TAIL`）**是系统对模型说的话**，所以它只在
            # 这条回灌里出现，`_emit` 一处都不许带上（见那个常量的说明）。
            if _says_no_checker(body):
                return (
                    for_prompt(
                        _no_checker_note(body)
                        + "这次改动没有可跑的机械判据。"
                        + _MODEL_ONLY_TAIL
                    ),
                    True,
                )
            # 成功那句**不能请它收尾**。验证只覆盖「这次改动的地方」，
            # 任务整体做完没有它并不知道；而一句「没有别的要改就收尾」，
            # 实测在 20 道题的铺盘里做完 5 道就让它宣布完成——
            # 它把「这一批做完」当成了「任务完成」。
            return (
                for_prompt(
                    f"系统自动跑了一遍测试：通过（{body}）。"
                    "这说的是这次改的地方通过了。"
                    + _MODEL_ONLY_TAIL
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

    def _emit_final_verify(self, payload: dict) -> None:
        """「最终验证」这一条**阶段行**：该在「已应用」之后被读到。

        最终验证验的是"待确认清单里那批内容"，而网页壳/桥/MCP 那条路上，
        用户要先点「应用」这一轮才算落地。当场发这条事件的话，用户看到的次序是

            最终验证 → 成功
            等你确认：1 处改动
            已应用 1 处改动

        ——一个阶段跑在自己前面（现场见
        `tests/fixtures/interact_probe/web-real-reports.md`）。所以 `--events`
        那条路由调用方给一个列表，把这一条**推迟**到结算之后发
        （`cli/commands/run.py::_events_mode`）。

        终端那条路没有"等确认"这一段（`settle` 与收尾在同一口气里落盘），
        照旧当场发：不传列表就走 `_emit`。
        """
        if self._deferred_final_verify is None:
            self._emit("tool", payload)
            return
        self._deferred_final_verify.append(dict(payload))

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

