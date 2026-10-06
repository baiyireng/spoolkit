from spoolkit.context.budget import OUTPUT_RESERVE_RATIO, Budget
from spoolkit.context.sections import Section


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
    """软线按窗口算、硬线按**有效预算**算，而且硬线不得高于有效预算。

    硬线这一条由 P0 取证改过来：旧口径 `window × 0.90` 在 8K 窗口下是 7372，
    而装配器**最多只能装到有效预算**（6964）——拿一个比它低得多的数当重置线，
    只会在历史还没装满时就把已读到的工具输出整段丢掉。真机对照 36 次里
    17 次至少重置过一次，重置之后模型从零重新收集素材，那是「素材够了却
    写不出结论」的主要成因（逐回合现场见
    `benchmarks/probes/orchestration/p0-turns.json`）。
    """
    b = Budget(window=1000)
    assert b.soft_limit() == int(1000 * 0.70)
    # 硬线 = 有效预算（850）× hard_trigger_ratio（0.98）= 833，且 ≤ effective()。
    assert b.hard_limit() == int(b.effective() * 0.98)
    assert b.hard_limit() <= b.effective()


def test_硬触发线随输出预留一起缩() -> None:
    """输出被抬到 boost 档之后有效预算变小，重置线必须跟着缩。

    否则抬输出这件事会顺带把「能装多少上下文」也悄悄放大——重置发生在
    `demand >= hard_limit()` 处，而 `demand` 是**裁剪前**的需求量，
    一个不跟着缩的线会让装配器在必留区段那一步直接抛错。
    """
    b = Budget(window=8192)
    before = b.hard_limit()
    b.boost_output()
    assert b.effective() < 8192
    assert b.hard_limit() < before
    assert b.hard_limit() <= b.effective()


def test_区段默认可裁剪() -> None:
    assert Section(name="code", text="x", priority=50).mandatory is False


def test_输出预留比例符合设计() -> None:
    assert OUTPUT_RESERVE_RATIO == 0.15

