"""写入门禁的判据用例（设计稿 §5.1 的 J1/J3/J4/J5）。

判据必须是代码而不是提示词：这组用例就是「哪些话不许进热记忆」的可执行版本。
正例与反例都列全（各 ≥6 条）——只钉反例会漏掉另一种回归：**判据过宽，
把真事实也拒了**。
"""

import pytest

from spoolkit import limits
from spoolkit.memory.gate import RULES, RULES_BY_ID, judge, sanitize

# 正例：真正的项目事实，一条都不许被拒。
ACCEPT_CASES: list[tuple[str, str, str]] = [
    ("fact", "测试命令是 pytest -q", ""),
    ("fact", "索引层用 ast 提取符号", ""),
    ("fact", "依赖只用标准库，零新依赖", ""),
    ("preference", "本仓统一用 uv 管理依赖", ""),
    ("decision", "检索先用 FTS5，不引入嵌入", ""),
    ("lesson", "解析自定义格式先上语法约束", "解析,正则"),
    ("fact", "热记忆的预算从 hot_memory_ratio 取值", ""),
]

# 反例：（类型, 文本, 触发词, 应当命中的判据编号）。
DECLINE_CASES: list[tuple[str, str, str, str]] = [
    ("fact", "你好！我是你的本地编程助手。有什么可以帮你的吗？", "", "J3"),
    ("fact", "感谢你的确认，我这就去改。", "", "J3"),
    ("fact", "你之前问过两次同样的问题。", "", "J3"),
    ("fact", "这次先跑一遍测试看看。", "", "J1"),
    ("fact", "任务已完成。请确认最终文件状态并宣告完成。", "", "J1"),
    ("fact", "运行方式：python -m spoolkit.cli.app --goal 目标", "", "J4"),
    ("fact", "| 城市 | 天气 |\n| 厦门 | 晴 |\n| 北京 | 多云 |", "", "J4"),
    ("fact", "结尾给一段代码：\n\n```python\nprint(1)\n```", "", "J4"),
    ("lesson", "解析自定义格式先上语法约束", "", "J5"),
]


@pytest.mark.parametrize(("kind", "text", "trigger"), ACCEPT_CASES)
def test_真事实不被判据误杀(kind: str, text: str, trigger: str) -> None:
    verdict = judge(kind, text, trigger)
    assert verdict.ok is True
    assert verdict.reason == ""


@pytest.mark.parametrize(("kind", "text", "trigger", "rule_id"), DECLINE_CASES)
def test_噪声条目被对应判据拒绝(
    kind: str, text: str, trigger: str, rule_id: str
) -> None:
    verdict = judge(kind, text, trigger)
    assert verdict.ok is False
    # 钉要点：理由里必须能读出是哪条判据拒的（不回显整句）。
    assert verdict.reason.startswith(rule_id)
    assert RULES_BY_ID[rule_id].title in verdict.reason


def test_判据表四条都带编号与一句理由() -> None:
    assert [rule.id for rule in RULES] == ["J1", "J3", "J4", "J5"]
    for rule in RULES:
        assert rule.title.strip()
        assert rule.check is not None


def test_超长条目按标定表的长度线拒绝() -> None:
    text = "天气表与交付答复的正文内容。" * 40
    assert len(text) > 400
    verdict = judge("fact", text)
    assert verdict.ok is False
    assert verdict.reason.startswith("J4")
    # 钉要点而不是整句：理由要提到长度，并点名是哪条标定线。
    assert "长度" in verdict.reason
    assert "remember_max_chars" in verdict.reason


def test_空文本被拒() -> None:
    verdict = judge("fact", "   ")
    assert verdict.ok is False
    assert "空条目" in verdict.reason


def test_长度阈值可被工作区覆盖() -> None:
    text = "测试命令是 pytest -q"
    assert judge("fact", text).ok is True
    verdict = judge("fact", text, overrides={"remember_max_chars": 10})
    assert verdict.ok is False
    assert verdict.reason.startswith("J4")


def test_表格行数阈值可被工作区覆盖() -> None:
    text = "| 城市 | 天气 |\n| 厦门 | 晴 |"
    assert judge("fact", text).ok is True
    verdict = judge("fact", text, overrides={"remember_table_lines": 2})
    assert verdict.ok is False
    assert verdict.reason.startswith("J4")


def test_阈值登记在标定表里() -> None:
    knob = limits.knob("remember_max_chars")
    assert knob is not None
    assert knob.default == 400
    assert knob.overridable is True

    table = limits.knob("remember_table_lines")
    assert table is not None
    assert table.default == 3
    assert table.overridable is True


def test_脱敏说出命中的形状但不回显值() -> None:
    secret = "sk-live-abcdefghijklmnop"
    cleaned, shapes = sanitize(f"部署时带上 {secret}，别写进记忆")
    assert secret not in cleaned
    assert shapes
    assert "供应商密钥前缀" in shapes[0]
    assert secret not in "".join(shapes)


def test_普通文本脱敏后原样返回() -> None:
    cleaned, shapes = sanitize("测试命令是 pytest -q")
    assert cleaned == "测试命令是 pytest -q"
    assert shapes == []


def test_脱敏不吞掉值后面的句子() -> None:
    """写入侧与读路径共用同一份形状表，所以「别删上下文」这条也一起继承。"""
    cleaned, _ = sanitize("SPOOLKIT_QQ_SECRET=abcdef123456，先放着")
    assert "abcdef123456" not in cleaned
    assert "先放着" in cleaned
    assert "SPOOLKIT_QQ_SECRET" in cleaned
