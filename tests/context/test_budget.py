from agents_dev.context.budget import OUTPUT_RESERVE_RATIO, Budget
from agents_dev.context.sections import Section


def test_输出预留被扣除() -> None:
    b = Budget(window=1000)
    assert b.output_reserve() == 150
    assert b.effective() == 850


def test_比例配额按有效预算计算() -> None:
    b = Budget(window=1000)
    assert b.quota("hot_memory") == int(850 * 0.15)
    assert b.quota("code") == int(850 * 0.35)


def test_系统区段使用固定配额() -> None:
    assert Budget(window=1000).quota("system") == 900
    assert Budget(window=32000).quota("system") == 900


def test_未知区段配额为零() -> None:
    assert Budget(window=1000).quota("不存在") == 0


def test_软硬两条触发线() -> None:
    b = Budget(window=1000)
    assert b.soft_limit() == int(1000 * 0.70)
    assert b.hard_limit() == int(1000 * 0.90)


def test_区段默认可裁剪() -> None:
    assert Section(name="code", text="x", priority=50).mandatory is False


def test_输出预留比例符合设计() -> None:
    assert OUTPUT_RESERVE_RATIO == 0.15

