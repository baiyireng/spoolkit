"""信号身份：发现类别与事件流 note 是两套名字。

混用的代价已经实测过一次：收尾重判的名单里放的是 note kind
``tests_weakened``，账本里的 finding kind 却是 ``test_removed`` /
``assert_loosened`` 这一族。名字对不上，旧判词撤不掉，留给 T12 去拦完成声明。

两套枚举故意分成两个类。字符串值可以碰巧相同（``behavior_changed``
既是发现也是 note），类型上仍然不许把 note 交给 ``forget_kind``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, StrEnum


class Audience(StrEnum):
    """谁在读这个标识。

    ``machine`` 必须在 ``scripts/``、``web/`` 或 ``tests/`` 里被引用至少一次
    （穷尽断言见 ``tests/agent/test_signals.py``）。``model`` / ``human``
    由主循环在进程内消费，不要求另有一份外部计数器。
    """

    MACHINE = "machine"
    MODEL = "model"
    HUMAN = "human"


class Family(Enum):
    """收尾时按哪一批作废重算。只有同一族会整批 ``forget_kind``。"""

    MECHANICAL = "mechanical"
    BEHAVIOR = "behavior"
    L3 = "l3"
    VERIFY = "verify"
    ESCALATION = "escalation"
    NOTE = "note"


MECHANICAL = Family.MECHANICAL
BEHAVIOR = Family.BEHAVIOR
L3 = Family.L3

_ESCALATION_KIND = re.compile(r"escalation_t\d+\Z")


class FindingKind(StrEnum):
    """账本里一条发现的判据名。收尾重判按这个撤，不按 note kind 撤。"""

    STALE_REFERENCE = "stale_reference"
    DELETED_REFERENCE = "deleted_reference"
    BROKEN_IMPORT = "broken_import"
    TEST_DELETED = "test_deleted"
    TEST_DATA_DELETED = "test_data_deleted"
    TEST_DATA_CHANGED = "test_data_changed"
    TEST_REMOVED = "test_removed"
    TEST_SKIPPED = "test_skipped"
    ASSERT_LOOSENED = "assert_loosened"
    FIXTURE_REMOVED = "fixture_removed"
    CONFTEST_COLLECTION_CHANGED = "conftest_collection_changed"
    PYTEST_ADDOPTS_WEAKENED = "pytest_addopts_weakened"
    PYTEST_CONFIG_CHANGED = "pytest_config_changed"
    PYTEST_CONFIG_DELETED = "pytest_config_deleted"
    BEHAVIOR_CHANGED = "behavior_changed"
    CLI_SURFACE_CHANGED = "cli_surface_changed"
    PERFORMANCE_REGRESSED = "performance_regressed"
    BEHAVIOR_NOT_CHECKED = "behavior_not_checked"
    SEMANTIC_FINDING = "semantic_finding"
    LANDING_IN_PROGRESS = "landing_in_progress"
    TAMPERING_SUSPECTED = "tampering_suspected"
    PROTECTED_WRITE = "protected_write"
    SCOPE_FALLBACK = "scope_fallback"
    ESCALATION_T1 = "escalation_t1"
    ESCALATION_T2 = "escalation_t2"
    ESCALATION_T3 = "escalation_t3"
    ESCALATION_T4 = "escalation_t4"
    ESCALATION_T5 = "escalation_t5"
    ESCALATION_T6 = "escalation_t6"
    ESCALATION_T7 = "escalation_t7"
    ESCALATION_T8 = "escalation_t8"
    ESCALATION_T9 = "escalation_t9"
    ESCALATION_T10 = "escalation_t10"
    ESCALATION_T11 = "escalation_t11"
    ESCALATION_T12 = "escalation_t12"
    ESCALATION_T13 = "escalation_t13"


class NoteKind(StrEnum):
    """事件流 note 的 kind。探针按它计数，不读散文。"""

    AUDIT = "audit"
    TESTS_WEAKENED = "tests_weakened"
    EVIDENCE_GAPS = "evidence_gaps"
    STALE_REFERENCES = "stale_references"
    BROKEN_IMPORT = "broken_import"
    BEHAVIOR_CHANGED = "behavior_changed"
    CLI_SURFACE_CHANGED = "cli_surface_changed"
    PERFORMANCE_REGRESSED = "performance_regressed"
    BEHAVIOR_NOT_CHECKED = "behavior_not_checked"
    BEHAVIOR_READINGS = "behavior_readings"
    SEMANTIC_FINDING = "semantic_finding"
    L3_READINGS = "l3_readings"
    L3_DETAIL = "l3_detail"
    L3_BLOCKED = "l3_blocked"
    ESCALATE = "escalate"
    ACTION_BLOCK = "action_block"
    BLOCKED_BY_REVIEW = "blocked_by_review"
    BLOCKED_BY_REVIEW_FINDING = "blocked_by_review_finding"
    PATH_TRIGGER = "path_trigger"
    STATE_VERIFY = "state_verify"


@dataclass(frozen=True)
class Signal:
    """一条已登记的信号：族、读者、这句话在说什么。"""

    family: Family
    audience: Audience
    meaning: str
    #: 发现发出去时用的 note。note 自己的登记项没有这一格。
    note: NoteKind | None = None


def _finding(
    family: Family, audience: Audience, meaning: str, note: NoteKind
) -> Signal:
    return Signal(family=family, audience=audience, meaning=meaning, note=note)


def _note(meaning: str, family: Family = Family.NOTE) -> Signal:
    return Signal(family=family, audience=Audience.MACHINE, meaning=meaning)


def _escalation_signals() -> dict[FindingKind, Signal]:
    """``escalation_tN`` 与 ``review_gate.TRIGGER_NOTES`` 同一批触发条件。

    严重度刻意是提示：高危会喂给 T12，升级自己再触发升级。
    """
    out: dict[FindingKind, Signal] = {}
    for kind in FindingKind:
        if _ESCALATION_KIND.fullmatch(kind.value) is None:
            continue
        trigger = kind.value.removeprefix("escalation_").upper()
        out[kind] = _finding(
            Family.ESCALATION,
            Audience.MODEL,
            f"触发条件 {trigger} 记进审查账本（提示档，不喂给 T12）",
            NoteKind.ESCALATE,
        )
    return out


SIGNALS: dict[FindingKind, Signal] = {
    FindingKind.STALE_REFERENCE: _finding(
        MECHANICAL,
        Audience.MODEL,
        "改名后旧名仍有残留",
        NoteKind.STALE_REFERENCES,
    ),
    FindingKind.DELETED_REFERENCE: _finding(
        MECHANICAL,
        Audience.MODEL,
        "删掉的文件或符号仍被引用",
        NoteKind.STALE_REFERENCES,
    ),
    FindingKind.BROKEN_IMPORT: _finding(
        MECHANICAL,
        Audience.MODEL,
        "导入的本地模块不存在",
        NoteKind.BROKEN_IMPORT,
    ),
    FindingKind.TEST_DELETED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "测试文件被删了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.TEST_DATA_DELETED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "测试数据被删了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.TEST_DATA_CHANGED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "测试数据被改了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.TEST_REMOVED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "测试用例被删了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.TEST_SKIPPED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "新增了跳过或 xfail",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.ASSERT_LOOSENED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "断言变少了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.FIXTURE_REMOVED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "conftest 里的 fixture 没了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.CONFTEST_COLLECTION_CHANGED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "conftest 新增了收集期构造",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.PYTEST_ADDOPTS_WEAKENED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "pytest 配置新增了跳过类开关",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.PYTEST_CONFIG_CHANGED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "pytest 配置里决定收集范围的键被改了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.PYTEST_CONFIG_DELETED: _finding(
        MECHANICAL,
        Audience.MODEL,
        "pytest 配置被删了",
        NoteKind.TESTS_WEAKENED,
    ),
    FindingKind.BEHAVIOR_CHANGED: _finding(
        BEHAVIOR,
        Audience.MODEL,
        "同一条命令、同一份输入，输出变了",
        NoteKind.BEHAVIOR_CHANGED,
    ),
    FindingKind.CLI_SURFACE_CHANGED: _finding(
        BEHAVIOR,
        Audience.MODEL,
        "公开符号或 CLI --help 被删改",
        NoteKind.CLI_SURFACE_CHANGED,
    ),
    FindingKind.PERFORMANCE_REGRESSED: _finding(
        BEHAVIOR,
        Audience.MODEL,
        "优化类任务变慢了，或行为变了还拿耗时当通过",
        NoteKind.PERFORMANCE_REGRESSED,
    ),
    FindingKind.BEHAVIOR_NOT_CHECKED: _finding(
        BEHAVIOR,
        Audience.MODEL,
        "行为回归这一步没能得出结果",
        NoteKind.BEHAVIOR_NOT_CHECKED,
    ),
    FindingKind.SEMANTIC_FINDING: _finding(
        L3,
        Audience.MODEL,
        "L3 语义审查报出的一条发现",
        NoteKind.SEMANTIC_FINDING,
    ),
    FindingKind.LANDING_IN_PROGRESS: _finding(
        Family.VERIFY,
        Audience.MODEL,
        "验证失败是改名还没铺完，不是代码失败",
        NoteKind.AUDIT,
    ),
    FindingKind.TAMPERING_SUSPECTED: _finding(
        Family.VERIFY,
        Audience.MODEL,
        "测试没过，同时业务代码被改短",
        NoteKind.AUDIT,
    ),
    FindingKind.PROTECTED_WRITE: _finding(
        Family.VERIFY,
        Audience.MODEL,
        "改动落在受保护路径上",
        NoteKind.AUDIT,
    ),
    FindingKind.SCOPE_FALLBACK: _finding(
        Family.VERIFY,
        Audience.HUMAN,
        "验证范围回退到项目根，结论以那一次为准",
        NoteKind.AUDIT,
    ),
    **_escalation_signals(),
}

NOTE_SIGNALS: dict[NoteKind, Signal] = {
    NoteKind.AUDIT: _note("验证性质：回退、中间态、迎合测试、越界写"),
    NoteKind.TESTS_WEAKENED: _note("测试被改弱（探针单独计数）"),
    NoteKind.EVIDENCE_GAPS: _note("完成声明没有可核对的证据"),
    NoteKind.STALE_REFERENCES: _note("残留引用与删除后仍被引用"),
    NoteKind.BROKEN_IMPORT: _note("导入的本地模块不存在"),
    NoteKind.BEHAVIOR_CHANGED: _note("对照运行输出变了"),
    NoteKind.CLI_SURFACE_CHANGED: _note("公开接口或 CLI 帮助文本变了"),
    NoteKind.PERFORMANCE_REGRESSED: _note("性能判据没过"),
    NoteKind.BEHAVIOR_NOT_CHECKED: _note("行为回归查不了"),
    NoteKind.BEHAVIOR_READINGS: _note("L2 的过程读数，不是发现"),
    NoteKind.SEMANTIC_FINDING: _note("L3 发现（同时进 review_findings）"),
    NoteKind.L3_READINGS: _note("L3 的过程读数，不是发现"),
    NoteKind.L3_DETAIL: _note("L3 发现的结构化字段"),
    NoteKind.L3_BLOCKED: _note("L3 的高严重度发现拦下了完成声明"),
    NoteKind.ESCALATE: _note("主动请示了一次"),
    NoteKind.ACTION_BLOCK: _note("危险动作被拦下或被评估"),
    NoteKind.BLOCKED_BY_REVIEW: _note("不许宣称完成"),
    NoteKind.BLOCKED_BY_REVIEW_FINDING: _note(
        "不许宣称完成，理由是真的有高严重度发现"
    ),
    NoteKind.PATH_TRIGGER: _note(
        "路径触发条件 T2/T3/T4/T6 的披露；成果照落盘，不是审查发现"
    ),
    NoteKind.STATE_VERIFY: Signal(
        family=Family.NOTE,
        audience=Audience.MODEL,
        meaning="任务状态里的待验证，每轮注入模型提示",
    ),
}


def _require(name: str, signal: Signal) -> None:
    if not isinstance(signal.family, Family):
        raise RuntimeError(f"{name} 缺 family")
    if not isinstance(signal.audience, Audience):
        raise RuntimeError(f"{name} 缺 audience")
    if not str(signal.meaning).strip():
        raise RuntimeError(f"{name} 缺 meaning")


def _check_registry() -> None:
    """导入期自检：每个枚举成员都登记了，且三件缺一就抛。

    两段循环的变量名故意不同：mypy 会把同一个名字先后绑定成
    ``FindingKind`` 和 ``NoteKind``，然后报 ``[assignment]``。
    """
    missing = [kind.value for kind in FindingKind if kind not in SIGNALS]
    if missing:
        raise RuntimeError("发现类别没登记：" + "、".join(missing))
    for finding_kind, finding_signal in SIGNALS.items():
        _require(finding_kind.value, finding_signal)
        if finding_signal.note is None:
            raise RuntimeError(f"{finding_kind.value} 没写它发出去的 note kind")
    missing_notes = [note.value for note in NoteKind if note not in NOTE_SIGNALS]
    if missing_notes:
        raise RuntimeError("note 类别没登记：" + "、".join(missing_notes))
    for note_kind, note_signal in NOTE_SIGNALS.items():
        _require(note_kind.value, note_signal)
    unknown = sorted(
        {
            finding_signal.note.value
            for finding_signal in SIGNALS.values()
            if finding_signal.note is not None
            and finding_signal.note not in NOTE_SIGNALS
        }
    )
    if unknown:
        raise RuntimeError("发现指向了没登记的 note：" + "、".join(unknown))


_check_registry()

#: `_mechanical_review` 产出的发现类别。收尾重判整批作废再按终态重算。
#: 这里是 finding kind，不是 note kind——``tests_weakened`` 进了这份名单
#: 就会撤不掉 ``test_removed`` 那一族。
MECHANICAL_KINDS: frozenset[FindingKind] = frozenset(
    kind for kind, signal in SIGNALS.items() if signal.family is MECHANICAL
)
WEAKENED_KINDS: frozenset[FindingKind] = frozenset(
    kind
    for kind, signal in SIGNALS.items()
    if signal.note is NoteKind.TESTS_WEAKENED
)
BEHAVIOR_KINDS: frozenset[FindingKind] = frozenset(
    kind for kind, signal in SIGNALS.items() if signal.family is BEHAVIOR
)
L3_KINDS: frozenset[FindingKind] = frozenset(
    kind for kind, signal in SIGNALS.items() if signal.family is L3
)
NOTE_KIND_BY_KIND: dict[FindingKind, NoteKind] = {
    kind: signal.note
    for kind, signal in SIGNALS.items()
    if signal.note is not None
}
