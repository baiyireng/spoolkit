from spoolkit.context.budget import OUTPUT_RESERVE_RATIO, Budget, compact_trigger
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
        "session_memory",
    }
    assert all(value > 0 for value in table.values())


def test_会话记忆占有效预算且不挤占系统与任务状态() -> None:
    large = Budget(window=32000)
    effective = large.effective()
    assert large.quota("session_memory") == int(effective * 0.08)
    small = Budget(window=1000)
    assert small.quota("system") == int(850 * 0.20)
    assert small.quota("task_state") == int(small.effective() * 0.05)
    assert small.quota("session_memory") == int(small.effective() * 0.08)


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


# ---- 素材压缩的动态阈值（O4 第一步） ----------------------------------------
#
# 这几条测的是**判据本身**：什么时候值得把已读到的素材压一遍、压完允许留多少。
# 真正的压缩动作在 `agent/material_compactor.py`（下一步），这里一个模型都不调、
# 一个文件都不碰。之所以要单独量它，是因为「什么时候压」这件事最容易被写死，
# 而写死的阈值会随窗口一起错——同一个「3000」在 8K 窗口下正好半屏，
# 在 2K 窗口下就是全部，而这两个窗口在本仓都会出现（探针 4096/8192，`--window` 可改）。


def test_素材压缩的整理线随窗口算出来() -> None:
    """同一个需求，4K 窗口该压、8K 窗口不该压。

    整理线 = `有效预算 − max(有效预算 × 28%, 800)`：4K 窗口（3482）算出 2508，
    8K 窗口（6964）算出 5015。这两个数是**算出来的，就该被冻在用例里**——
    谁把它改成写死的常数，谁先让这条红。
    """
    small = compact_trigger(Budget(window=4096), used=2800, material=1500)
    large = compact_trigger(Budget(window=8192), used=2800, material=1500)
    assert (small.effective, small.threshold) == (3482, 2508)
    assert (large.effective, large.threshold) == (6964, 5015)
    assert small.should is True
    assert large.should is False
    assert small.expected_saving > 0
    # 不压就是不压：素材原样留着，省下的 token 记 0（不许报一个没发生的收益）
    assert (large.expected_saving, large.keep_tokens) == (0, 1500)


def test_余量还够的时候不压素材() -> None:
    decision = compact_trigger(Budget(window=4096), used=1200, material=1500)
    assert decision.should is False
    assert decision.free == 3482 - 1200
    assert "整理线" in decision.reason


def test_素材太短不值得花一次整理() -> None:
    """过线了、素材占比也够，但素材本身只有 400 token。

    一次整理要花一次模型调用，省下的还不够调用本身贵——这条防抖的意义就在这。
    """
    decision = compact_trigger(Budget(window=4096), used=3400, material=400)
    assert decision.should is False
    assert "下限" in decision.reason


def test_素材占需求太少就不值当() -> None:
    """需求大但大在系统提示与检索上时，压素材腾不出多少地方。

    这里 `used=3400`（已过 2508 的线）、素材 700 token 也过了 600 的下限，
    但它只占需求的 20%——那是「为压缩而压缩」，不压，并且**要把百分比报出来**。
    """
    decision = compact_trigger(Budget(window=4096), used=3400, material=700)
    assert decision.should is False
    assert "21%" in decision.reason


def test_压完允许留多少是按缺口算的() -> None:
    """4K、`used=2800`、素材 1500：余量 682，离目标余量 974 差 292。

    于是「最多留 1500 − 292 = 1208」，并保证压完余量**刚好回到目标**
    （682 + 292 ≥ 974）——不是压得越狠越好，压过头就是把还要用的素材扔掉。
    """
    d = compact_trigger(Budget(window=4096), used=2800, material=1500)
    assert (d.free, d.reserve_target) == (682, 974)
    assert (d.keep_tokens, d.expected_saving) == (1208, 292)
    assert d.free + d.expected_saving >= d.reserve_target


def test_小窗口下保底余量比比例线更早生效() -> None:
    """窗口 2048：比例线给的目标余量只有 487，保底（800）把它抬起来。

    抬高的直接后果是**整理线从 1253 提前到 941**：小窗口里一条工具输出就可能
    上千 token（`max_output_chars=4000` 字符），等 1253 再动手已经没地方放了。
    """
    b = Budget(window=2048)
    d = compact_trigger(b, used=1000, material=900)
    assert (d.effective, d.reserve_target, d.threshold) == (1741, 800, 941)
    assert d.threshold < int(1741 * 0.72)  # 比例线是 1253，保底把它提前了
    assert d.should is True


def test_缺口比素材还大时只压到下限并如实报() -> None:
    """素材 880 token，缺口 800：压完只剩 80，低于 600 的下限。

    于是只要求砍到下限（600），并**如实承认回不到目标**——`free + 省下的`
    只有 280 < 800，这件事在数字上可查。留 600 而不是「一条不留」，是因为
    判据保护区由压缩器逐字保留，纯函数的职责只是给预算，不是替它决定丢什么。
    """
    d = compact_trigger(Budget(window=2048), used=1741, material=880)
    assert d.should is True
    assert (d.keep_tokens, d.expected_saving) == (600, 280)
    assert d.free + d.expected_saving < d.reserve_target


def test_触发时机能被本次运行的覆盖改掉() -> None:
    """同一个需求与素材，把比例调到 0.3 就从「不压」变「该压」。

    这条同时证明两件事：阈值是活的（走 `.agent/limits.json` 与命令行覆盖），
    而且它**真的跟着比例走**（8K 下整理线从 5015 前移到 2090）。
    """
    plain = compact_trigger(Budget(window=8192), used=2800, material=1500)
    tuned = compact_trigger(
        Budget(window=8192, overrides={"material_compact_ratio": 0.3}),
        used=2800,
        material=1500,
    )
    assert (plain.should, plain.threshold) == (False, 5015)
    assert (tuned.should, tuned.threshold) == (True, 2090)

