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

# 用户第 3 条质疑（意图要含「想拿什么 + 接下来怎么做」）与第 5 条发现
# （replace_lines 的行号锚点）落在提示词里的那几处。丢了就是行为变了。
INTENT_ANCHORS = {
    "SYSTEM": [
        "想拿到什么",
        "拿到之后接下来怎么做",
        "不算意图",
    ],
    "WORKFLOW_EDIT_LINES": [
        "expect",
        "锚点对不上就会被拒绝",
    ],
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


def test_意图口径与行号锚点一条不缺() -> None:
    """改了提示词就必须有的第二组机械核对（占位符核对见上）。

    这一组是「意图要含想拿什么 + 接下来怎么做」与「replace_lines 必须带
    expect」这两条要求的落点：它们只写在提示词里，没有别的地方会提醒。
    """
    missing: list[str] = []
    for name, anchors in INTENT_ANCHORS.items():
        text = getattr(T, name)
        missing += [f"{name}:{one}" for one in anchors if one not in text]
    assert not missing, f"语义锚点缺: {missing}"


def test_版本号与改动记录同步() -> None:
    """升了版本就必须有那一行记录：不然「结果归因到某一版」是空话。"""
    source = Path(T.__file__).read_text(encoding="utf-8")
    assert f'PROMPT_VERSION = "{T.PROMPT_VERSION}"' in source
    assert T.PROMPT_VERSION in source.split("# 改动记录", 1)[1]


# 探针首轮（5 个形状 3 绿 2 红）抓出的两个提示层缺陷，各自对应的锚点。
# 它们是「丢了就是行为变了」的那几句，所以同样机械核对。
PROBE_ANCHORS = {
    # merge-html 红：工具集里没有删除文件的工具，模型只能让用户自己 rm。
    "WORKFLOW_DELETE": [
        "delete_file",
        "不删目录",
        "待确认",
        "revert",
        "不要建议用户自己去 rm",
        "假装删除",
    ],
    # rename-cross-file 红：只改了 .py，README / docs 没动却说「已统一」。
    "WORKFLOW_RENAME": [
        "README",
        "docs",
        "残留清单",
        "统一",
        "与事实不符",
    ],
}


def test_探针轮新增的锚点一条不缺() -> None:
    missing: list[str] = []
    for name, anchors in PROBE_ANCHORS.items():
        text = getattr(T, name)
        missing += [f"{name}:{one}" for one in anchors if one not in text]
    assert not missing, f"语义锚点缺: {missing}"


def test_探针轮新增的两段没有引入新的占位符() -> None:
    """新增的两段是纯文本：工具名与参数名写死在里面。

    一旦它们被写成 `{delete_tool}` 这种占位符，`SYSTEM.format(tools=..., workflow=...)`
    会在运行期 KeyError——而那条路只在真的注册了删除工具的工作区里才走到。
    """
    for name in PROBE_ANCHORS:
        assert _placeholders(name) == [], name
