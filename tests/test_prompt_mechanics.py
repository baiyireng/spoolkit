"""提示词的机械核对：改提示词必做。

两件事，都是机器能判的：
1. **占位符一个不丢**：`{name}` 形态的 format 占位符集合与改造前一致（丢一个
   就是运行期 KeyError，或者更糟——把 `{goal}` 原样发给模型）。
2. **语义锚点一个不缺**：那些「丢了就是行为变了」的原句片段逐条 `assert`。

改造前的占位符清单在这里逐字冻结（从 2026-10-03.2 那一版抓的）。
"""

import re
from pathlib import Path

from spoolkit.context import templates as T

# 改造前（PROMPT_VERSION 2026-10-03.2）全部模板里的占位符集合，逐字冻结。
FROZEN_PLACEHOLDERS = {
    "SYSTEM": ["tools", "workflow"],
    "WORKFLOW_HEAD": ["lines"],
    "WORKFLOW_FIND": ["names"],
    "WORKFLOW_READ_ROOTS": ["roots"],
    "OUTPUT_TRUNCATED": ["limit"],
    "SUPERVISE": [
        "asking", "goal", "step", "limit", "ceiling", "room", "state", "trace",
        "edits", "calls", "resets", "repeats", "no_edit", "tokens", "cycle",
        "oscillations", "same_query",
    ],
    "DECOMPOSE": ["goal", "context", "limit"],
    "DECOMPOSE_CONTINUE": ["planned", "start", "ask"],
    "REFLECT": ["goal", "results"],
    "DECOMPOSE_BATCH_LIMIT": ["limit"],
    "EXTEND": ["done", "reflection", "start", "ask"],
    "DECOMPOSE_FILL": ["missing", "ask"],
    "DISPATCH": ["goal", "context"],
    "JUDGE_REVIEW": ["acceptance", "review"],
    "REPAIR": ["goal", "acceptance", "reasons"],
    "DISTILL": ["body", "limit"],
}

# 「丢了就是行为变了」的原句片段。分两类：这一轮**新加**的（确认通道），
# 与**原来就在**的（护栏、便宜路径、验证分派）。
NEW_ANCHORS = {
    "WORKFLOW_REWRITE_DECLARE": [
        "rewrite_reason",
        "confirm_rewrite",
        "只发项 id",
        "不重发正文",
        "acked",
        "dropped",
        "项 id",
        "不会落盘",
        "acked_disappeared",
        "不得原样重提",
        "消失清单",
        "令牌",
        "没有令牌",
    ],
}
KEPT_ANCHORS = {
    "WORKFLOW_REWRITE_DECLARE": ["合并", "去重", "搬运", "rewrite_reason"],
    "WORKFLOW_EDIT_LINES": ["replace_lines", "输出预算"],
    "WORKFLOW_VERIFY": ["verify_file", ".html", ".js", ".py"],
    "WORKFLOW_RUN": ["run_command", "自动验证"],
    "WORKFLOW_ACT": ["重复两次以上"],
    "WORKFLOW_WRITE_SAFE": ["不用停下来等确认"],
    "EMPTY_TURN_FEEDBACK": ["done"],
}


def _placeholders(name: str) -> list[str]:
    text = getattr(T, name)
    return sorted(set(re.findall(r"\{([a-z_]+)\}", text)))


def test_占位符与改造前逐字一致() -> None:
    for name, expected in FROZEN_PLACEHOLDERS.items():
        assert _placeholders(name) == sorted(expected), name


def test_新锚点一条不缺() -> None:
    for name, anchors in NEW_ANCHORS.items():
        text = getattr(T, name)
        missing = [one for one in anchors if one not in text]
        assert not missing, f"{name} 缺: {missing}"


def test_旧锚点一条不缺() -> None:
    for name, anchors in KEPT_ANCHORS.items():
        text = getattr(T, name)
        missing = [one for one in anchors if one not in text]
        assert not missing, f"{name} 缺: {missing}"


def test_版本号与改动记录同步() -> None:
    """升了版本就必须有那一行记录：不然「结果归因到某一版」是空话。"""
    source = Path(T.__file__).read_text(encoding="utf-8")
    assert f'PROMPT_VERSION = "{T.PROMPT_VERSION}"' in source
    assert T.PROMPT_VERSION in source.split("# 改动记录", 1)[1]
