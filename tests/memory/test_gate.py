"""写入门禁的判据用例（设计稿 §5.1 的 J1/J3/J4/J5）。

判据必须是代码而不是提示词：这组用例就是「哪些话不许进热记忆」的可执行版本。
正例与反例都列全（各 ≥6 条）——只钉反例会漏掉另一种回归：**判据过宽，
把真事实也拒了**。
"""

import pytest

from spoolkit import limits
from spoolkit.memory.gate import (
    RULES,
    RULES_BY_ID,
    NormalizePolicy,
    content_hash,
    judge,
    normalize_key,
    normalize_policy,
    sanitize,
)

# 正例：真正的项目事实，一条都不许被拒。
ACCEPT_CASES: list[tuple[str, str, str]] = [
    ("fact", "测试命令是 pytest -q", ""),
    ("fact", "索引层用 ast 提取符号", ""),
    ("fact", "依赖只用标准库，零新依赖", ""),
    ("preference", "本仓统一用 uv 管理依赖", ""),
    ("decision", "检索先用 FTS5，不引入嵌入", ""),
    ("lesson", "解析自定义格式先上语法约束", "解析,正则"),
    ("fact", "热记忆的预算从 hot_memory_ratio 取值", ""),
    # 新加的「交付模板小标题」判据不许过宽：`**加粗小标题**` 才算交付正文，
    # 平铺的一句真事实（"构建命令是 uv run --frozen"）必须留下。
    ("fact", "构建命令是 uv run --frozen", ""),
    ("fact", "CLI 的帮助文案写在 docs/cli.md 里", ""),
]

# 反例：（类型, 文本, 触发词, 应当命中的判据编号）。
DECLINE_CASES: list[tuple[str, str, str, str]] = [
    ("fact", "你好！我是你的本地编程助手。有什么可以帮你的吗？", "", "J3"),
    ("fact", "感谢你的确认，我这就去改。", "", "J3"),
    ("fact", "你之前问过两次同样的问题。", "", "J1"),
    ("fact", "这次先跑一遍测试看看。", "", "J1"),
    ("fact", "任务已完成。请确认最终文件状态并宣告完成。", "", "J1"),
    ("fact", "运行方式：python -m spoolkit.cli.app --goal 目标", "", "J4"),
    ("fact", "| 城市 | 天气 | 温度 |\n| 厦门 | 晴 | 30 |\n| 北京 | 多云 | 18 |", "", "J4"),
    ("fact", "结尾给一段代码：\n\n```python\nprint(1)\n```", "", "J4"),
    # 真实工作区 memory.md:9/10 的三种交付正文形状：围栏与正文**同行**
    # （原先只认行首围栏，这三条全漏）、以及带 `**运行方式：**` 小标题的。
    ("fact", "文件为 `snake.py`。**运行方式：** ``` python snake.py ```", "", "J4"),
    ("fact", "网页版已经写好了，文件为 `snake.html`。**运行方式：** 用浏览器打开", "", "J4"),
    ("lesson", "解析自定义格式先上语法约束", "", "J5"),
]


def test_代码围栏判据认任意位置的围栏() -> None:
    """围栏**与正文同行**也要判掉（真实工作区 `memory.md:9/10` 就是这个形状）。

    只认行首围栏的写法对这些条目全部漏判——它们不是"以围栏开头"，而是
    "正文里带一段围栏"。这条用例把理由钉到"含代码围栏"这句话上：把正则
    改回 `^\\s*(```|~~~)`（`re.MULTILINE`）这里就红。
    """
    inline_fence = "贪吃蛇小游戏已经实现完毕，文件为 `snake.py`。 **运行方式：** ``` python snake.py ```"
    verdict = judge("fact", inline_fence)
    assert verdict.ok is False
    assert verdict.reason.startswith("J4")
    assert "含代码围栏" in verdict.reason

    # 多行写法（围栏自己一行）同样判掉。
    block_fence = "结尾给一段代码：\n\n```python\nprint(1)\n```"
    assert "含代码围栏" in judge("fact", block_fence).reason


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


def test_表格判据与表格行数阈值() -> None:
    """表格判据的口径：**一行里 ≥3 个格就是一张表**（单行表也算）。

    热记忆的条目是一条物理行，所以"≥3 行同构表格"这条阈值对单行表永远不成立；
    真实工作区 `memory.md:7` 的天气表就是靠长度线才被判掉的。这里把口径钉死：
    单行三格 = 表 → J4；两格并列 = 不是表 → 放行。
    """
    one_row = "天气： | 城市 | 最低温 | 最高温 | 天气状况 |"
    assert judge("fact", one_row).ok is False

    three_rows = "| 城市 | 天气 | 温度 |\n| 厦门 | 晴 | 30 |\n| 北京 | 多云 | 18 |"
    assert judge("fact", three_rows).reason.startswith("J4")

    # 两格并列不是表（`a | b` 是"两个字段"的常见写法，不许被误杀）。
    two_cells = "范围 | 说明：这一栏讲的是 remember_table_lines 的含义"
    assert judge("fact", two_cells).ok is True


def test_表格行数阈值可被工作区覆盖() -> None:
    """阈值仍是登记表里的可覆盖项（多行表按行数判）。"""
    knob = limits.knob("remember_table_lines")
    assert knob is not None and knob.default == 3 and knob.overridable is True

    two_rows = "| 城市 | 天气 | 温度 |\n| 厦门 | 晴 | 30 |"
    assert judge("fact", two_rows).ok is False  # 单行表口径以外：两行也已是表


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


# ---- 归一化与去重指纹（设计稿 §4.3 / §5.3 ④ 第 1 级：精确级）----
#
# 这一组用例只回答一个问题：**什么算同一条**。口径要写清、可参数化，
# 所以正例（该等价的都等价）与反例（不该等价的一条都不许撞）都要有。


def test_默认归一化口径来自登记表() -> None:
    """口径不是一个藏在函数体里的常量：四个开关都在 limits 里登记。"""
    policy = normalize_policy()
    assert policy.casefold is True
    assert policy.collapse_ws is True
    assert policy.strip_punct is True
    assert policy.hash_chars == int(limits.resolve("dedup_hash_chars")[0])
    assert policy == NormalizePolicy(
        casefold=True, collapse_ws=True, strip_punct=True, hash_chars=16
    )


def test_归一化去掉大小写空白与标点() -> None:
    assert normalize_key(" 测试命令是 pytest -q。 ") == "测试命令是pytestq"
    assert normalize_key("测试命令是 PYTEST-Q") == "测试命令是pytestq"
    # 空白怎么算由开关定：默认整体去掉，折成空格时才区分「a b」与「ab」。
    assert normalize_key("测试命令是 pytest -q") == normalize_key("测试命令是pytest-q")
    assert normalize_key("a b", {"dedup_collapse_ws": 0}) == "a b"
    assert normalize_key("  a   b  ", {"dedup_collapse_ws": 0}) == "a b"


def test_归一化把全角转成半角() -> None:
    assert normalize_key("（uv）") == normalize_key("(uv)")
    assert normalize_key("ＡＢＣ") == normalize_key("abc")


def test_归一化去掉零宽与软连字符() -> None:
    """这些字符肉眼不可见，却会让两条本该相同的文本各走一条。"""
    assert normalize_key("pytest\u200b-q") == normalize_key("pytest-q")
    assert normalize_key("pytest\u00ad-q") == normalize_key("pytest-q")


def test_归一化不动汉字与假名() -> None:
    """全角转半角只覆盖 ASCII 那一档：汉字/假名不许被卷进来。"""
    assert normalize_key("测试命令是 pytest -q") == "测试命令是pytestq"
    assert normalize_key("ひらがな") == "ひらがな"


def test_归一化不改写正文() -> None:
    """归一化只用于比较：judge 看到的仍是原文（否则判据会被悄悄改掉）。"""
    assert judge("fact", "你好！我是你的本地编程助手。").ok is False


def test_指纹对等价写法相同() -> None:
    digest = content_hash("测试命令是 pytest -q")
    assert content_hash(" 测试命令是  pytest -q。 ") == digest
    assert content_hash("测试命令是 PYTEST-Q") == digest


def test_指纹对不同事实不同() -> None:
    assert content_hash("测试命令是 pytest -q") != content_hash("构建命令是 uv build")


def test_指纹长度来自登记表() -> None:
    assert len(content_hash("测试命令是 pytest -q")) == 16
    overridden = content_hash("测试命令是 pytest -q", {"dedup_hash_chars": 8})
    assert len(overridden) == 8
    assert content_hash("测试命令是 pytest -q").startswith(overridden)


def test_空文本指纹为空字符串() -> None:
    """归一化后为空就没有可比内容：空指纹不许和另一条空条目互相去重。"""
    assert content_hash("   ") == ""
    assert content_hash("！！！") == ""
    assert content_hash("测试命令是 pytest -q") != ""


def test_去重口径可被工作区覆盖() -> None:
    """口径要能按工作区调：调一版去重不该改代码（本仓既有规矩）。"""
    assert normalize_policy({"dedup_casefold": 0}).casefold is False
    assert normalize_key("ABC", {"dedup_casefold": 0}) == "ABC"
    # 关掉去标点：标点参与比较，两条就不再等价。
    assert normalize_key("测试命令是 pytest -q。") != normalize_key(
        "测试命令是 pytest -q。", {"dedup_strip_punct": 0}
    )
    assert content_hash("a b", {"dedup_collapse_ws": 0}) != content_hash(
        "ab", {"dedup_collapse_ws": 0}
    )


def test_去重口径登记在标定表里() -> None:
    for name in ("dedup_casefold", "dedup_collapse_ws", "dedup_strip_punct"):
        knob = limits.knob(name)
        assert knob is not None, name
        assert knob.default == 1
        assert knob.overridable is True
    hash_knob = limits.knob("dedup_hash_chars")
    assert hash_knob is not None
    assert hash_knob.default == 16
    assert hash_knob.overridable is True
