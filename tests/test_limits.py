"""能力标定值的登记、覆盖与「来源可查」。

用户提的原则：不要太多写死的数量上限——外部供应商比本地模型强，那些限制
反而压低它的发挥。判据是「这个数字只会因为模型更强而被突破吗？」（设计文档 2.7）。

这张表要回答两个具体问题：**现在生效的是多少、从哪来**，以及**不改代码
能不能动它**。
"""

from pathlib import Path

import pytest

from spoolkit import limits
from spoolkit.config import Config
from spoolkit.limits import CAPABILITY, SAFETY


def test_默认值来自登记表() -> None:
    assert limits.resolve("repeat_block_at") == (3.0, "默认")
    assert limits.knob("repeat_block_at").kind == CAPABILITY


def test_覆盖之后来源要说成配置() -> None:
    """来源必须说得出来——否则调参就是猜，这和写死没区别。"""
    value, source = limits.resolve("repeat_block_at", {"repeat_block_at": 5})
    assert (value, source) == (5.0, "配置")


def test_安全边界不提供覆盖() -> None:
    """它防的是不可逆后果，和模型强弱无关——不该有「调大一点」这种选项。"""
    assert limits.knob("path_confinement").kind == SAFETY
    value, source = limits.resolve("path_confinement", {"path_confinement": 0})
    assert (value, source) == (1.0, "默认")


def test_没登记过的名字要报错() -> None:
    with pytest.raises(KeyError):
        limits.resolve("这个不存在")


def test_覆盖可以落盘再读回(tmp_path: Path) -> None:
    limits.save_overrides(tmp_path, {"repeat_block_at": 5})
    assert limits.load_overrides(tmp_path) == {"repeat_block_at": 5.0}


def test_坏文件不影响运行(tmp_path: Path) -> None:
    path = limits.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{这不是 JSON", encoding="utf-8")
    assert limits.load_overrides(tmp_path) == {}


def test_配置按名字取值() -> None:
    config = Config(project_root=Path("."), overrides={"repeat_block_at": 5})
    assert config.limit("repeat_block_at") == 5
    assert config.limit("no_edit_limit") == 4  # 没覆盖的用默认


def test_渲染把来源和能不能改都写出来() -> None:
    text = limits.render({"repeat_block_at": 5})
    assert "repeat_block_at" in text
    assert "配置" in text and "可覆盖" in text
    assert "写死" in text  # 安全边界那一类


def test_预算比例也走覆盖() -> None:
    """预算比例是这套系统里影响最大的能力标定值：它决定每次请求装多少。

    默认值按本机 27B + 8K 窗口实测，换个模型或换个窗口就不该照搬。
    """
    from spoolkit.context.budget import Budget

    plain = Budget(8192)
    wide = Budget(8192, overrides={"soft_trigger_ratio": 0.5, "code_ratio": 0.5})
    assert plain.soft_limit() == int(8192 * 0.70)
    assert wide.soft_limit() == 4096
    assert wide.quota("code") > plain.quota("code")


def test_热记忆的占比只有一个出处() -> None:
    """同一个概念两处各写一份，比没有这个旋钮更糟。

    装配层（budget.quota("hot_memory")）和裁剪层（hot_budget）说的是同一件事；
    只搬一处的话，`limits --set hot_memory_ratio=0.2` 会改一半、留一半。
    """
    from spoolkit.context.budget import Budget
    from spoolkit.memory.hot import hot_budget

    overrides = {"hot_memory_ratio": 0.2}
    assert hot_budget(8192, overrides=overrides) == int(
        int(8192 * 0.85) * 0.2
    )
    assert Budget(8192, overrides=overrides).quota("hot_memory") == int(
        8192 * 0.85 * 0.2
    )


# P1（记忆架构）计划 Task 1 一次登记、批次 2–4 分批消费。名单里是**还没接线**
# 的那 21 个（括号里是计划点名的消费者与任务号）；`recall_limit` 不在这里——
# 它在本批 T2 的 `memory/semantic.py` 里就接了线。名单是 **T1 的快照**，
# 不是永久豁免：后续批次把某一项接上线之后，请把它从这里删掉。
#
# 用「⊆」（见 `test_表里列的每一项都真的接上了`）而不是「==」，是因为本用例
# 分辨不了「还没接线」与「接线的那一批还没做」——写成「==」会给批次 2–4 留下
# 一条必然变红的用例。名单之外任何 capability 项仍然必须接线。
P1_PENDING_WIRING: tuple[str, ...] = (
    "w_bm25",  # T5/T6
    "w_embed",  # T6
    "rank_w_relevance",  # T6
    "rank_w_recency",  # T6
    "rank_w_importance",  # T6
    "rank_w_usage",  # T6
    "rank_w_penalty",  # T6
    "rank_recency_half_life_days",  # T6
    "recall_candidates_n",  # T6
    "recall_rerank_k",  # T6
    "recall_entry_chars",  # T11
    "dedupe_similarity",  # T7
    "remember_min_chars",  # T7
    "memory_budget_ratio",  # T12
    "event_chain_max_units",  # T11
    "event_chain_render_max_chars",  # T11
    "state_direct_inject_min_confidence",  # T12
    "state_direct_inject_max",  # T12
    "state_direct_inject_max_age_days",  # T12
    "state_auto_pick",  # T9
    "event_archive_threshold",  # T3/T9
)


def test_表里列的每一项都真的接上了() -> None:
    """登记表最怕的是「列了但没接线」——那比不列更糟：它会骗人。

    这条不是洁癖：**我自己就犯过一次**——上一轮把预取预算列进「已搬」的名单，
    而那段批量替换其实没匹配上、却打印了 done，等于登记表在骗人。
    所以这里**穷尽地**检查：每个 capability / mechanical 项都必须在代码里
    被取用一次（安全项是符号项——它们描述的是一条规则，不是一个数值）。

    P1 Task 1 是**一次登记、分批消费**（计划「并行与串行」的单写者清单把
    `limits.py` 钉成单写点）：那 22 个新旋钮里，`recall_limit` 在本批就接线，
    其余 21 个要等批次 2–4 的消费者落地，名单见 `P1_PENDING_WIRING`。
    """
    sources = [
        path
        for path in Path("src/spoolkit").rglob("*.py")
        if path.name != "limits.py"
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    # 名单本身不许写错名字（写错了这里先红，而不是在 missing 里被悄悄放行）。
    assert set(P1_PENDING_WIRING) <= {entry.name for entry in limits.KNOBS}

    missing = []
    for entry in limits.KNOBS:
        assert entry.note.strip(), f"{entry.name} 没写它管什么"
        if entry.kind == SAFETY:
            continue  # 符号项：它约束的是行为，不是可调数值
        if f'"{entry.name}"' in text:
            continue
        if entry.name in P1_PENDING_WIRING:
            continue  # P1 已登记、消费者在批次 2–4
        missing.append(entry.name)
    assert not missing, f"表里列了但代码里没人取用：{missing}"


# ---- P1（记忆架构）旋钮一次登记：计划 Task 1 -----------------------------
#
# P1 计划 Task 1 把 P1 需要的旋钮**一次性**登记进 `KNOBS`（之后批次 2–4 只
# 消费不追加，`limits.py` 因此可以从所有人的写范围里摘出去）。这里的清单是
# 计划里点名的那 22 个新增项，默认值与依据（设计稿章节 / 计划任务号）一并
# 冻结在这里：**改默认值或漏登记一个都会红**。
P1_NEW_KNOBS: tuple[tuple[str, float], ...] = (
    ("w_bm25", 1.0),  # §14.4 规矩 1；本计划初值，不是设计稿数值
    ("w_embed", 0.0),  # §13.11 / §13.13 第 6 条：P1 不引嵌入
    ("rank_w_relevance", 0.40),  # §6.1
    ("rank_w_recency", 0.20),  # §6.1
    ("rank_w_importance", 0.25),  # §6.1
    ("rank_w_usage", 0.10),  # §6.1
    ("rank_w_penalty", 0.05),  # §6.1
    ("rank_recency_half_life_days", 30),  # §6.1
    ("recall_candidates_n", 50),  # §14.4 规矩 3（口径裁决 3 改名）
    ("recall_rerank_k", 5),  # §14.4 规矩 3（硬约束 K ≤ recall_limit）
    ("recall_limit", 5),  # §6.2 表
    ("recall_entry_chars", 240),  # §6.2 表（对齐 history_snippet_chars）
    ("dedupe_similarity", 0.90),  # §5.3 ④ / §6.1
    ("remember_min_chars", 8),  # §4.3 容量上限段
    ("memory_budget_ratio", 0.30),  # §6.2 表
    ("event_chain_max_units", 2),  # §6.7 / §6.2
    ("event_chain_render_max_chars", 400),  # §6.7 / §6.2
    ("state_direct_inject_min_confidence", 0.8),  # §6.6 / §13.13 第 7 条
    ("state_direct_inject_max", 1),  # §6.6
    ("state_direct_inject_max_age_days", 30),  # §13.13 第 7 条；本计划初值
    ("state_auto_pick", 0),  # §15.6 第 3 条（默认关）
    ("event_archive_threshold", 1000),  # §13.13 第 5 条（待标定初值）
)

# 改造前（P0 收尾时）`KNOBS` 的全部名字，逐字冻结：**一个都不许丢**
# （Task 1 验收 2）。改造后 = 这 62 个 + 上面 22 个。
KNOBS_BEFORE_P1: tuple[str, ...] = (
    "repeat_warn_at",
    "repeat_block_at",
    "repeat_intervene_at",
    "no_edit_limit",
    "no_edit_intervene_at",
    "empty_turn_limit",
    "step_ceiling_factor",
    "supervisor_max_grant",
    "name_cycle_window",
    "name_cycle_at",
    "oscillation_at",
    "same_query_at",
    "done_inline",
    "max_scopes",
    "max_targets",
    "review_limit",
    "survey_budget",
    "output_reserve_ratio",
    "output_reserve_boost_ratio",
    "soft_trigger_ratio",
    "hard_trigger_ratio",
    "hot_memory_ratio",
    "task_state_ratio",
    "code_ratio",
    "retrieval_ratio",
    "lessons_ratio",
    "recent_chat_turns",
    "recent_chat_chars",
    "history_digest_turns",
    "history_digest_chars",
    "history_search_limit",
    "history_search_max",
    "history_snippet_chars",
    "episode_final_chars",
    "remember_max_chars",
    "remember_table_lines",
    "dedup_casefold",
    "dedup_collapse_ws",
    "dedup_strip_punct",
    "dedup_hash_chars",
    "hot_max_items",
    "hot_item_chars",
    "hot_importance_long_step",
    "system_quota",
    "fixed_quota_cap",
    "compact_threshold",
    "compact_code_boost",
    "max_index_files",
    "index_seconds",
    "max_output_chars",
    "prefetch_budget",
    "prefetch_content_budget",
    "decompose_material_budget",
    "decompose_output_budget",
    "research_page_limit",
    "research_output_budget",
    "research_steps",
    "search_result_limit",
    "command_timeout",
    "max_command_timeout",
    "path_confinement",
    "denied_commands",
)


# T11 补缺轮补登的两个旋钮：T11 交回来时把「时序完整度系数 0.5」与「事件侧逐字
# 召回门槛 2」写成了裸常数——两条都是**本实现的裁决**（设计稿 §6.7 表只说"缺基准
# 要扣分"、判据②只说"逐字召回要把键带回来"，都没给数），但裁决不等于可以写死：
# 本仓的规矩是阈值必须进登记表可覆盖。名单与 `P1_NEW_KNOBS` 分开列，是因为它们
# 不属于 P1 Task 1 那一次登记（那是 22 项的快照，改它会把"22"这个出处弄脏）。
T11_GAP_KNOBS: tuple[tuple[str, float], ...] = (
    ("event_chain_incomplete_factor", 0.5),  # §6.7 表 / §15.6；T11 的实现裁决
    ("event_chain_min_matched_tokens", 2),  # §6.7 判据②；T11 的实现裁决
)


# 「三道墙」那一轮补登的旋钮（大重写声明通道、失败重提硬拦、步数预算随规模放大、
# 被拦的动作不占预算）。同样单列：它们不属于 P1 的登记批次，混进去会把
# 「P1 Task 1 登记了 22 项」这个出处弄脏。
WALL_KNOBS: tuple[tuple[str, float], ...] = (
    ("failed_repeat_block_at", 2),  # 失败过的调用第一次原样重提即硬拦
    ("step_lines_per_bonus", 60),  # 每 60 行目标产出给 1 步额外预算
    ("step_lines_bonus_cap", 12),  # 最多补 12 步
    ("blocked_refund_limit", 3),  # 被拦的编辑动作最多退还 3 步
)


def test_p1_旋钮全部登记且可覆盖() -> None:
    """Task 1 验收 1：清单里每个名字都在表里、是 CAPABILITY、默认值如冻结。"""
    for name, default in P1_NEW_KNOBS + T11_GAP_KNOBS + WALL_KNOBS:
        entry = limits.knob(name)
        assert entry is not None, f"{name} 没登记"
        assert entry.kind == CAPABILITY, f"{name} 不是能力标定值"
        assert entry.overridable is True, f"{name} 不可覆盖"
        assert entry.default == default, f"{name} 默认值不是 {default}"
        assert entry.note.strip(), f"{name} 没写依据"


def test_p1_点名的第23行是P0既有项而不是重复登记() -> None:
    """Task 1 的旋钮表共 23 行，第 15 行 `retrieval_ratio=0.15` 是 P0 的既有项。

    它**不重复登记**：`_BY_NAME` 是字典，重名会静默取后者（`limits.py:324`）。
    这条同时是「22 个新增」这个数目的出处。
    """
    assert limits.resolve("retrieval_ratio") == (0.15, "默认")
    assert "retrieval_ratio" in KNOBS_BEFORE_P1  # 它是既有项，不是新增项
    assert "retrieval_ratio" not in dict(P1_NEW_KNOBS)


def test_既有旋钮一个不少() -> None:
    """Task 1 验收 2：改造前的名字集合是改造后的子集（集合包含断言）。"""
    names = {entry.name for entry in limits.KNOBS}
    assert set(KNOBS_BEFORE_P1) <= names
    assert set(KNOBS_BEFORE_P1) & set(dict(P1_NEW_KNOBS)) == set()
    assert set(KNOBS_BEFORE_P1) & set(dict(T11_GAP_KNOBS)) == set()
    assert set(KNOBS_BEFORE_P1) & set(dict(WALL_KNOBS)) == set()


def test_旋钮没有重名() -> None:
    """Task 1 验收 5：重名会让 `_BY_NAME` 静默取后者。

    总数 = P0 的 62 + P1 Task 1 的 22 + T11 补缺轮的 2（`T11_GAP_KNOBS`）
    + 「三道墙」那一轮的 4（`WALL_KNOBS`）。
    """
    assert len(limits.KNOBS) == len(limits._BY_NAME)
    assert len(limits.KNOBS) == (
        len(KNOBS_BEFORE_P1)
        + len(P1_NEW_KNOBS)
        + len(T11_GAP_KNOBS)
        + len(WALL_KNOBS)
    )


def test_步数预算随规模放大且可按工作区覆盖() -> None:
    """「三道墙」之一：目标产出越大，步数预算给得越多——而且这个口径可覆盖。

    它必须是可覆盖的标定值：60 行/步是按本机 dogfood 的一次实测取的一倍余量，
    换模型/换窗口就该能动（改 `.agent/limits.json` 即可，不必改代码）。
    """
    from spoolkit.agent.loop import scaled_step_bonus

    def budget(lines: int, overrides: dict | None = None) -> int:
        return scaled_step_bonus(
            Config(project_root=Path("."), overrides=overrides or {}), lines
        )

    # 小文件不补：10 行的目标不值得多给预算。
    assert budget(10) == 0
    # 规模上去就补，且按 cap 封顶。
    assert budget(300) == 5
    assert budget(10_000) == 12
    # 覆盖改了就变：每 30 行给一步 → 300 行给 10 步（不再按 60 折算）。
    assert budget(300, {"step_lines_per_bonus": 30}) == 10
    # 封顶也能覆盖，而且盖得住上面那条。
    assert (
        budget(10_000, {"step_lines_per_bonus": 30, "step_lines_bonus_cap": 4}) == 4
    )
    # 关掉这条路（0 行/步）就不补——它是排查用的开关。
    assert budget(300, {"step_lines_per_bonus": 0}) == 0


def test_p1旋钮可以按工作区覆盖(tmp_path: Path) -> None:
    """Task 1 验收 3/4：`.agent/limits.json` 覆盖得动，`w_embed` 默认必须是 0.0。"""
    limits.save_overrides(tmp_path, {"rank_w_relevance": 0.5, "w_embed": 0.25})
    overrides = limits.load_overrides(tmp_path)

    assert limits.resolve("rank_w_relevance", overrides) == (0.5, "配置")
    # 覆盖是逐项的：没被覆盖的仍是默认。
    assert limits.resolve("rank_w_recency", overrides) == (0.2, "默认")
    assert "rank_w_relevance" in limits.render(overrides)

    # 定案：嵌入不进 P1，所以 `w_embed` 的**默认**是 0.0（上面那行证明它可覆盖）。
    assert limits.resolve("w_embed") == (0.0, "默认")
    assert limits.knob("w_embed").default == 0.0
