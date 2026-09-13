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


def test_表里列的每一项都真的接上了() -> None:
    """登记表最怕的是「列了但没接线」——那比不列更糟：它会骗人。

    这条不是洁癖：**我自己就犯过一次**——上一轮把预取预算列进「已搬」的名单，
    而那段批量替换其实没匹配上、却打印了 done，等于登记表在骗人。
    所以这里**穷尽地**检查：每个 capability / mechanical 项都必须在代码里
    被取用一次（安全项是符号项——它们描述的是一条规则，不是一个数值）。
    """
    sources = [
        path
        for path in Path("src/spoolkit").rglob("*.py")
        if path.name != "limits.py"
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    missing = []
    for entry in limits.KNOBS:
        assert entry.note.strip(), f"{entry.name} 没写它管什么"
        if entry.kind == SAFETY:
            continue  # 符号项：它约束的是行为，不是可调数值
        if f'"{entry.name}"' not in text:
            missing.append(entry.name)
    assert not missing, f"表里列了但代码里没人取用：{missing}"
