from agents_dev.context.budget import OUTPUT_RESERVE_RATIO, Budget
from agents_dev.context.sections import Section


def test_输出预留被扣除() -> None:
    b = Budget(window=1000)
    assert b.output_reserve() == 150
    assert b.effective() == 850


def test_比例配额按有效预算计算() -> None:
    # 用大窗口验证基准比例；小窗口会触发向代码倾斜的调整，另有测试覆盖。
    b = Budget(window=32000)
    effective = b.effective()
    assert b.quota("hot_memory") == int(effective * 0.15)
    assert b.quota("code") == int(effective * 0.35)


def test_系统区段使用固定配额() -> None:
    # 大窗口下用满固定额度
    assert Budget(window=32000).quota("system") == 900
    # 小窗口下必须按比例封顶，否则系统提示会吃掉全部预算
    assert Budget(window=1000).quota("system") == int(850 * 0.20)


def test_固定额度随窗口伸缩() -> None:
    small = Budget(window=2000).quota("system")
    large = Budget(window=64000).quota("system")
    assert small < large
    assert large == 900


def test_小窗口下把配额从历史挪向代码() -> None:
    small = Budget(window=2000)
    large = Budget(window=32000)
    assert small.quota("code") > int(small.effective() * 0.35)
    assert large.quota("code") == int(large.effective() * 0.35)


def test_分配表可查询() -> None:
    table = Budget(window=8192).allocation()
    assert set(table) == {
        "system",
        "hot_memory",
        "task_state",
        "retrieval",
        "code",
        "lessons",
    }
    assert all(value > 0 for value in table.values())


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

