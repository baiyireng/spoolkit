"""能力标定值的登记表。

系统里有六十来个数字常量，它们不是一类东西。判据只有一句（见设计文档 2.7）：

> **这个数字只会因为模型更强而被突破吗？**

是 → 它是**能力标定**，必须可调，而且默认不该压低——同一个数字用在强模型上
就是天花板。外部 API 的窗口、预算与判断力都比本机小模型强得多，替它砍掉
能力是纯损失。

防的是不可逆后果 → 它是**安全边界**，写死，不提供覆盖入口。

这张表的作用不是「集中管理」这种好听的话，而是回答两个具体问题：

1. **现在生效的是多少、从哪来**——一条看不见来源的限制，用起来和写死的
   限制一样难受：你不知道该不该动它、动到多少合适。
2. **不改代码能不能动它**——能力标定的值可以按工作区覆盖
   （`.agent/limits.json`），试一版不用改代码、也不用重装。

安全边界也列在这里，但标成不可覆盖：把「哪些能改」摆明，比让人去猜强。
"""

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Mapping

CAPABILITY = "capability"
MECHANICAL = "mechanical"
SAFETY = "safety"


@dataclass(frozen=True)
class Knob:
    """一个标定值。"""

    name: str
    default: float
    kind: str
    note: str

    @property
    def overridable(self) -> bool:
        return self.kind != SAFETY


# 默认值是**按本机 27B + 20 步预算**实测出来的那一套。换模型时该动的就是
# 这里面的 capability 项——这也是它们必须可覆盖的理由。
KNOBS: tuple[Knob, ...] = (
    Knob(
        "repeat_warn_at", 2, CAPABILITY,
        "同一个调用连续重复几次开始提醒（实测 7B 会连续 10 次一字不差）",
    ),
    Knob(
        "repeat_block_at", 3, CAPABILITY,
        "重复到几次不再执行：第三次中间没有任何别的动作，结果不可能变",
    ),
    Knob(
        "repeat_intervene_at", 4, CAPABILITY,
        "重复到几次升级给督导（机械层管不住的那部分交给判断）",
    ),
    Knob(
        "no_edit_limit", 4, CAPABILITY,
        "连续几步不提出改动就收窄成只能写（实测模型对文字提醒免疫）",
    ),
    Knob(
        "no_edit_intervene_at", 8, CAPABILITY,
        "漫游到几步升级给督导：每步换一个查询的绕法，机械计数器抓不到",
    ),
    Knob(
        "empty_turn_limit", 4, CAPABILITY,
        "连续几个空回合判它卡死并收尾（协议里没有「我在等你确认」的表达）",
    ),
    Knob(
        "step_ceiling_factor", 8, CAPABILITY,
        "督导续期的总上限 = 基础步数 × 这个倍数（安全线，不是任务预算）",
    ),
    Knob(
        "supervisor_max_grant", 20, CAPABILITY,
        "督导一次最多给多少步：给太多等于取消上限",
    ),
    Knob(
        "done_inline", 8, CAPABILITY,
        "状态块里「已完成」列几条；全量在 .agent/progress.md（每轮注入 = 每个请求的税）",
    ),
    Knob(
        "max_scopes", 5, CAPABILITY,
        "一次改多处时最多逐处验几个范围：再散，验证比任务本身还贵",
    ),
    Knob(
        "max_targets", 10, CAPABILITY,
        "一次派发最多带几件（超出实现者会烧光预算、交回半成品）",
    ),
    Knob(
        "review_limit", 5, CAPABILITY,
        "派发超过几件就提醒「审查者容易给不出结论」（实测 8 件：活全做对、审查没结论）",
    ),
    Knob(
        "survey_budget", 1200, CAPABILITY,
        "survey 一次最多收多少 token 的上下文",
    ),
    Knob(
        "output_reserve_ratio", 0.15, CAPABILITY,
        "为输出预留的窗口比例；被截断后抬到 boost（比例，随窗口伸缩）",
    ),
    Knob(
        "output_reserve_boost_ratio", 0.30, CAPABILITY,
        "截断过一次之后本次任务的输出预算上限",
    ),
    Knob(
        "soft_trigger_ratio", 0.70, CAPABILITY,
        "上下文达到窗口的多少就开始整理历史（不打断任务）",
    ),
    Knob(
        "hard_trigger_ratio", 0.90, CAPABILITY,
        "达到多少直接重置上下文（保留任务状态）",
    ),
    Knob(
        "hot_memory_ratio", 0.15, CAPABILITY,
        "热记忆占有效预算的比例（每轮注入）",
    ),
    Knob("task_state_ratio", 0.05, CAPABILITY, "任务状态块占有效预算的比例"),
    Knob(
        "code_ratio", 0.35, CAPABILITY,
        "代码片段占有效预算的比例：小窗口时会自动再加一档（把配额从历史挪向代码）",
    ),
    Knob("retrieval_ratio", 0.15, CAPABILITY, "检索内容占有效预算的比例"),
    Knob("lessons_ratio", 0.10, CAPABILITY, "历史教训占有效预算的比例"),
    Knob("system_quota", 900, CAPABILITY, "系统提示的固定配额（token）"),
    Knob(
        "fixed_quota_cap", 0.20, CAPABILITY,
        "固定配额也得随窗口伸缩：小窗口下它最多只能占有效预算的两成",
    ),
    Knob(
        "compact_threshold", 3000, CAPABILITY,
        "有效预算低于这个值就进入小窗口模式（配额从历史挪向代码）",
    ),
    Knob("compact_code_boost", 0.10, CAPABILITY, "小窗口模式下代码配额额外加多少"),
    Knob(
        "max_index_files", 2000, CAPABILITY,
        "超过这么多 Python 文件就不建索引（先数一遍再决定，数一遍很便宜）",
    ),
    Knob("index_seconds", 20.0, CAPABILITY, "建索引的时间预算，到点停下并说明"),
    Knob(
        "max_output_chars", 4000, CAPABILITY,
        "工具输出回灌给模型时最多留多少字符（超出的截掉并标注）",
    ),
    Knob("prefetch_budget", 400, CAPABILITY, "开局预取（符号表）花多少 token"),
    Knob(
        "prefetch_content_budget", 1000, CAPABILITY,
        "开局预取还带上多少 token 的代码片段",
    ),
    Knob("command_timeout", 180, MECHANICAL, "单条命令的默认超时"),
    Knob("max_command_timeout", 600, SAFETY, "命令超时上限：防止挂死"),
    Knob("path_confinement", 1, SAFETY, "写操作不出工作区（不可覆盖）"),
    Knob("denied_commands", 1, SAFETY, "永久禁止清单：提权与系统级操作（不可覆盖）"),
)

_BY_NAME = {knob.name: knob for knob in KNOBS}


def knob(name: str) -> Knob | None:
    return _BY_NAME.get(name)


def resolve(name: str, overrides: Mapping[str, float] | None = None) -> tuple[float, str]:
    """返回值与**来源**。来源要能说出来，否则调参就是猜。"""
    entry = _BY_NAME.get(name)
    if entry is None:
        raise KeyError(f"没有登记过这个标定值: {name}")
    if entry.overridable and overrides and name in overrides:
        return float(overrides[name]), "配置"
    return float(entry.default), "默认"


def path_for(project_root: Path) -> Path:
    return project_root / ".agent" / "limits.json"


def load_overrides(project_root: Path) -> dict[str, float]:
    path = path_for(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found[str(key)] = float(value)
    return found


def save_overrides(project_root: Path, overrides: Mapping[str, float]) -> Path:
    path = path_for(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(sorted(overrides.items())), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def render(overrides: Mapping[str, float] | None = None) -> str:
    """现在生效的是多少、从哪来。"""
    lines = [f"{'值':>8}  {'来源':<6} {'能不能改':<8} 名字", "-" * 72]
    for entry in KNOBS:
        value, source = resolve(entry.name, overrides)
        shown = f"{value:g}"
        can = "可覆盖" if entry.overridable else "写死"
        lines.append(f"{shown:>8}  {source:<6} {can:<8} {entry.name}")
        lines.append(f"{'':>8}  {entry.note}")
    return "\n".join(lines)
