"""每件工具的能力元数据：**单一事实来源**。

## 为什么要有这一层

「这件工具会读吗 / 会写吗 / 会出网吗 / 会不会不可逆 / 要不要请批 / 能不能
放进隔离体」这些事实原先散在几处**手写清单**里：

- `agent/loop.py`：`EDIT_TOOLS` / `WRITE_TOOLS` / `COMMIT_TOOLS` /
  `PROGRESS_TOOLS`（还有一份已经删掉的 `_REFUND_TOOLS`——它现在按「这一步
  有没有真的执行」判定，与工具名无关）；
- `agent/review_gate.py`：`ACTION_TOOLS` / `NETWORK_TOOLS` /
  `NETWORK_ENTRY_TOOLS` / `WRITE_TOOLS`。

散着的代价是**漂移**：漏写一个名字不会有任何报错，症状是隐性的（实测
`find_symbol` 不在退还名单里，被硬拦之后照旧吃掉 1 步）。所以这里把每件
工具的**能力**收成一份声明，上面那些常量一律变成它的**派生视图**——加一把
工具只需要在这里加一行，`tests/tools/test_capabilities.py` 会在漏加时变红。

## 这一层的纪律

1. **只声明事实，不做判定**。这一层不 import `agent/**`，也不改任何判定
   结果：某一轮里「该拦的还拦、该退的还退、该请批的还请批」。
2. **`params` 不另造一套**：参数名从工具**既有的**参数 schema
   （`ToolSpec.parameters`）里取，见 `params_of` / `of_spec`。
3. **名字不许写错**：注册表遍历用例（缺声明就红 + 表里没有幽灵）钉住两边。

## 字段的两个用途

- 已有的判定按派生视图取值（`EDIT_TOOLS` … `NETWORK_ENTRY_TOOLS`），
  与改动前逐条相同（冻结快照用例保证）；
- 编排层将来直接用 `can_parallel` / `requires_serial` / `needs_isolation`
  这类纯函数判断「能不能并行 / 要不要隔离」。本轮**只留接口，不实现**
  任何批量执行或并行调度。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "Capability",
    "UNKNOWN",
    "ACTION_TOOLS",
    "APPROVAL_TOOLS",
    "COMMIT_TOOLS",
    "EDIT_TOOLS",
    "ISOLATABLE_TOOLS",
    "NETWORK_ENTRY_TOOLS",
    "NETWORK_TOOLS",
    "PROGRESS_TOOLS",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "can_parallel",
    "declared",
    "declared_names",
    "needs_isolation",
    "of_spec",
    "params_of",
    "requires_serial",
]


@dataclass(frozen=True)
class Capability:
    """一件工具的结构性能力。

    字段都是**事实**，不是判定：编排层拿它们判断「能不能并行 / 要不要隔离 /
    重复有没有意义 / 需不需要批准」。每一项的含义写在各字段下面。
    """

    #: 会读工作区里的内容（文件 / 目录 / 符号 / 搜索 / 统计）。
    #: 记忆、诊断报告、工具索引这些**旁路**不算工作区内容。
    reads: bool = False

    #: 会导致工作区内容被改写：落盘，或进入「待确认改动」暂存。
    #: 只往旁路台账写一笔的工具（记忆、诊断请求）不算——它们不改工作区。
    writes: bool = False

    #: 会出网（真实网络副作用）。只读本地网页缓存的 `web_read` 不算。
    network: bool = False

    #: 主循环里**真的会出网**的那个入口动作。`web_fetch` / `web_search`
    #: 只发给研究会话（它们不在主循环的注册表里），所以不是入口；`research`
    #: 是。这一位就是原来 `review_gate.NETWORK_ENTRY_TOOLS` 的那一条口径。
    network_entry: bool = False

    #: 会**不可逆地**动用户的东西：删文件、迁移文件、执行任意命令。
    #: 这一档就是原来 `review_gate.ACTION_TOOLS` 的口径——「拦的是动作」。
    destructive: bool = False

    #: 执行前应当得到用户批准（命令审批通道 / 破坏性动作的拦截 / 出网升级）。
    needs_approval: bool = False

    #: 这件工具的工作**可以在隔离执行体里完成**（子会话 / 试跑副本 / 沙箱），
    #: 主循环只收结论。改工作区的工具不能隔离（它改的就是这份共享工作区）；
    #: 读工作区的工具也不隔离（它读的正是主循环眼前那份内容）。
    isolatable: bool = False

    #: 会**派生一个手里有写工具的执行体**（子智能体那一套）。它自己这一层的
    #: 参数里没有路径，所以它**不是**写工具（保持 `writes=False`：进不了
    #: `EDIT_TOOLS` / `WRITE_TOOLS`，既有判定逐条不变）；但编排层要拿这一位
    #: 回答「这一条能不能与别的调用并行」——两条并行的派发会同时往同一份
    #: 待确认清单里写。`dispatch` 是唯一一个为真的。
    delegates: bool = False

    #: 会把**已暂存的正文**落盘（确认入口）。它不是「交内容」的写工具，
    #: 所以不算进 `EDIT_TOOLS` 那一档，但算「把改动推进下去」（`COMMIT_TOOLS`）。
    commits: bool = False

    #: 成功即算一项「产出」，进状态块的已完成清单。读 / 查是手段不是产出。
    #: 这一档就是原来 `loop.PROGRESS_TOOLS` 的口径。
    progress: bool = False

    #: **参数名**，从这件工具既有的参数 schema（`ToolSpec.parameters`）里取，
    #: 不另造一份 schema。结构性声明里留空，由 `of_spec()` 在注册表那一侧绑定。
    params: tuple[str, ...] = ()


#: 没声明过的工具（外挂 MCP 工具的名字由用户的配置决定，编译期不可能穷举）
#: 按**最保守**的一份处理：会读、会写、会出网、会破坏、要请批、也不假设它
#: 搬不动。宁可少并行、多隔离、多请批，不可反过来。
UNKNOWN = Capability(
    reads=True,
    writes=True,
    network=True,
    destructive=True,
    needs_approval=True,
    isolatable=True,
)


def params_of(parameters: dict[str, Any]) -> tuple[str, ...]:
    """从**既有参数 schema** 里取参数名——不新造一套。

    取的是 `properties` 的键，也就是工具索引里逐字出现的那些参数名；
    拿不到（schema 形状不对、外挂工具没给）就给空元组，不编。
    """
    if not isinstance(parameters, dict):
        return ()
    properties = parameters.get("properties")
    return tuple(properties) if isinstance(properties, dict) else ()


# ---- 每件工具的能力声明（**唯一一份**清单）----------------------------------
#
# 顺序即声明顺序，派生的元组视图按它排列：`COMMIT_TOOLS` 的顺序会决定
# 收窄 schema 里参数的出场顺序，也就是**提示词逐字**；所以编辑类工具的
# 声明顺序必须与改动前 `WRITE_TOOLS + (CONFIRM_TOOL,)` 的顺序一致
# （replace_lines / replace_text / write_file / delete_file / confirm_rewrite）。
_DECLARED: dict[str, Capability] = {
    # ---- 看 / 查：只读工作区 ----
    "read_file": Capability(reads=True),
    "list_dir": Capability(reads=True),
    "search_code": Capability(reads=True),
    "dir_stats": Capability(reads=True),
    "survey": Capability(reads=True),
    "find_symbol": Capability(reads=True),
    "file_symbols": Capability(reads=True),
    "find_callers": Capability(reads=True),
    "read_diagnosis": Capability(reads=True),
    "verify_file": Capability(reads=True),
    # ---- 量 / 核 / 记：不碰工作区（旁路台账不算）----
    "calc": Capability(isolatable=True),
    "tool_help": Capability(),
    "check_numbers": Capability(),
    "request_diagnosis": Capability(),
    "recall": Capability(),
    "remember": Capability(),
    "search_history": Capability(),
    # ---- 改：交内容的写工具（顺序即收窄 schema 的顺序）----
    "replace_lines": Capability(writes=True, progress=True),
    "replace_text": Capability(writes=True),
    "write_file": Capability(writes=True, progress=True),
    "delete_file": Capability(
        writes=True,
        destructive=True,
        needs_approval=True,
        progress=True,
    ),
    # 确认入口：它自己不交内容，但会让**已暂存的正文**落盘。
    "confirm_rewrite": Capability(writes=True, commits=True),
    # ---- 跑：破坏性动作，跑在试跑副本里 ----
    "run_command": Capability(
        destructive=True,
        needs_approval=True,
        isolatable=True,
        progress=True,
    ),
    # ---- 派 / 网：丢给隔离执行体 ----
    # 编排：**一步内批量执行多个工具调用**。它在声明表里是一位中性项——它
    # 自己不动工作区，动工作区的是它**批里的分支**，而每条分支按**那个工具
    # 自己的**声明取判据（`can_parallel` / `requires_serial` / `needs_isolation`
    # / `needs_approval`，见 `tools/orchestrate.py`）。所以它不写、不读、不
    # 出网、不请批：派的那些派生视图（EDIT/WRITE/COMMIT/ACTION/NETWORK/
    # APPROVAL/ISOLATABLE/READ）因此一条都不变——那份冻结快照不用动。
    "orchestrate": Capability(),
    "dispatch": Capability(isolatable=True, delegates=True),
    "research": Capability(
        network=True,
        network_entry=True,
        needs_approval=True,
        isolatable=True,
    ),
    "web_read": Capability(isolatable=True),
    "web_fetch": Capability(network=True, needs_approval=True, isolatable=True),
    "web_search": Capability(network=True, needs_approval=True, isolatable=True),
    # ---- 历史动作名 ----
    # 注册表里已经没有这两把工具（迁移/改名由编辑工具承担），但判定口径仍在：
    # `review_gate.ACTION_TOOLS` 由 `destructive` 派生，少了它们会**静默松开**
    # 那两个名字上的动作拦截。所以它们留在声明里，并由冻结快照用例钉住。
    "move_file": Capability(destructive=True, needs_approval=True),
    "rename_file": Capability(destructive=True, needs_approval=True),
}


def declared(name: str) -> Capability | None:
    """这件工具**声明过**能力吗：没声明返回 `None`。

    注册表遍历用例靠它「缺声明就红」——将来加工具忘了声明，第一步就报错。
    """
    return _DECLARED.get(name)


def declared_names() -> tuple[str, ...]:
    """声明过的全部工具名（含两个历史动作名，见上面的注释）。"""
    return tuple(_DECLARED)


def of_spec(spec: Any) -> Capability:
    """注册表里一件工具的**完整**能力：结构性声明 + 复用它的参数 schema。

    没声明过的（外挂工具）只带回 `params`，其余字段按 `UNKNOWN` 走——
    见 `can_parallel` 这几个函数的取法。
    """
    base = _DECLARED.get(str(getattr(spec, "name", "")))
    bound = params_of(getattr(spec, "parameters", {}) or {})
    return Capability(params=bound) if base is None else replace(base, params=bound)


# ---- 派生视图 ---------------------------------------------------------------
#
# 一条规则：**这里不许再出现工具名字面量**。任何「哪几把工具算……」都写成
# 对上面那份声明的筛选，于是清单不会再漂移。


def _by(predicate: Any) -> tuple[str, ...]:
    """按声明顺序筛出满足 `predicate` 的工具名。"""
    return tuple(name for name, cap in _DECLARED.items() if predicate(cap))


#: 交内容的写工具：不具破坏性，也不是「确认已暂存内容」的入口。
#: （`review_gate` 里那个同名常量就是这一档——它只记录、不拦。）
EDIT_TOOLS = _by(lambda c: c.writes and not c.destructive and not c.commits)

#: 「把工作区变成我要的样子」的写工具：交内容的 + 删除。
#: 删除必须与写工具并列：`edited` / `changed_paths` / 自动验证的范围都按它判，
#: 漏了它就会出现「删掉了文件，但系统认为这一步什么都没做」。
WRITE_TOOLS = _by(lambda c: c.writes and not c.commits)

#: 「把改动推进下去」的工具：一切会让工作区内容被改写的调用（含确认入口）。
#: 确认入口必须留在这一档：漫游太久时采样空间会收窄成「只能写」，
#: 收窄之后拿掉它，模型就再也确认不了那份暂存的正文了。
COMMIT_TOOLS = _by(lambda c: c.writes)

#: 成功即算一项「产出」的动作（写进状态块的已完成清单）。
#: 只用于成员判断，**顺序不参与任何输出**（改动前那份的顺序与这里不同）。
PROGRESS_TOOLS = _by(lambda c: c.progress)

#: 「拦的是动作，不是成果」那一档：破坏性动作。用 frozenset——判定只做成员检查。
ACTION_TOOLS = frozenset(_by(lambda c: c.destructive))

#: 出网工具（不含主循环那两个入口）。
NETWORK_TOOLS = frozenset(_by(lambda c: c.network and not c.network_entry))

#: 主循环里**真的会出网**的那个入口动作。
NETWORK_ENTRY_TOOLS = frozenset(_by(lambda c: c.network_entry))

#: 执行前应当请批的工具（命令、删除 / 迁移这类动作、出网）。
APPROVAL_TOOLS = frozenset(_by(lambda c: c.needs_approval))

#: 可以在隔离执行体里跑的工具。
ISOLATABLE_TOOLS = frozenset(_by(lambda c: c.isolatable))

#: 会读工作区的工具。
READ_TOOLS = frozenset(_by(lambda c: c.reads))


# ---- 给编排层留的纯函数（本轮只留接口，不实现调度）--------------------------


def _capability(name: str) -> Capability:
    """按名字取能力声明；没声明过的（外挂工具）退回最保守的一份。"""
    return _DECLARED.get(name, UNKNOWN)


def requires_serial(name: str) -> bool:
    """这次调用会改动共享状态（工作区）→ 必须与其它调用串行。"""
    cap = _capability(name)
    return cap.writes or cap.destructive


def can_parallel(name: str) -> bool:
    """这次调用不动共享状态 → 可以并行（就是 `requires_serial` 的反面）。"""
    return not requires_serial(name)


def needs_isolation(name: str) -> bool:
    """有外部副作用（出网 / 破坏性）而且搬得动 → 放进隔离执行体里跑。

    搬不动的（例如删除工作区文件：它要动的就是这份工作区）不走隔离这一条，
    由批准通道把关——隔离它没有意义，只是把动作挪个地方。
    """
    cap = _capability(name)
    return (cap.network or cap.destructive) and cap.isolatable
