"""数字可追溯性：交付物里的数字要在工具输出里找得到出处。

测试数据取自一次真实运行——它交出的清理报告里有两处数字与自己的数据矛盾，
而且方向相反（一个往上编、一个往下压）。这道检查要能把它们挑出来，
同时不能误伤那些正确引用的数字。
"""

from agents_dev.check.numbers import claims_in, render, untraceable

# 真实运行里工具输出的片段（dir_stats 的结果）
SOURCES = [
    "D:\\workSpace 合计：483832 个文件，67.0 GB 按直接子项（大小降序）："
    " 18.6 GB 68038 个文件 AnimaLoraStudio 13.1 GB 2 个文件 models"
    " 9.1 GB 61017 个文件 pyvideotrans 8.4 GB 64744 个文件 tauri_player"
    " 5.8 GB 6825 个文件 world_game",
    "D:\\workSpace\\pyvideotrans 合计：61017 个文件，9.1 GB "
    "7.6 GB 60360 个文件 .venv 719.5 MB 88 个文件 .git",
    "D:\\workSpace\\tauri_player 合计：64744 个文件，8.4 GB "
    "5.1 GB 5921 个文件 src-tauri 3.2 GB 53293 个文件 service",
]


def test_抽出带单位的数字主张() -> None:
    claims = claims_in("模型权重 13.1 GB，构建产物 1.0 GB，共 58119 个 .pyc")
    assert [claim.value for claim in claims] == [13.1, 1.0, 58119.0]
    assert claims[0].unit == "GB"
    assert claims[-1].line == 1


def test_能挑出凭空多出来的数字() -> None:
    """工具输出里根本没有这个数——多半是心算出来的合计，或者编的。"""
    found = untraceable("预计可释放 42 GB。", SOURCES)
    assert [claim.text for claim in found] == ["42 GB"]


def test_已知边界_数字存在但引错对象时拦不住() -> None:
    """**这条是边界，不是能力**，写下来免得以后误以为它管得更宽。

    它只查「这个数有没有出处」，不查「这个数是不是这个对象的」。
    实测那两处错误里有一半属于后者：报告写 target 约 8 GB，而源里
    8.4 GB 是 tauri_player 整体的数、5.1 GB 才是 src-tauri 的——
    8 与 8.4 只差 5%，够得着。

    要拦这一类，得让工具的**输出可解析**：有办法把「对象 → 数值」的
    对应关系抽出来，才谈得上核对它有没有配错。
    """
    assert untraceable("`src-tauri\\target` 约 8 GB。", SOURCES) == []


def test_能挑出拿局部当整体() -> None:
    """报告把 1.2 GB 的一个 dll 说成整个 venv，而 venv 是 7.6 GB。"""
    report = "`pyvideotrans\\.venv\\` 约 1.2 GB，可随时重建。"
    found = [claim.text for claim in untraceable(report, SOURCES)]
    assert any("1.2 GB" in item for item in found), found


def test_正确引用的数字不会被误伤() -> None:
    report = (
        "总量 67.0 GB / 483832 个文件；tauri_player 整体 8.4 GB，"
        "其中 src-tauri 5.1 GB；pyvideotrans 的 .venv 是 7.6 GB。"
    )
    assert untraceable(report, SOURCES) == []


def test_同精度抄写算有出处() -> None:
    """`13.10 GB` 与 `13.1 GB` 是同一个数，不该被当成编造。"""
    assert untraceable("模型权重 13.10 GB。", ["models 13.1 GB"]) == []


def test_换单位取整算有出处() -> None:
    """实测误报过一次：报告写 `.map` 预计 0.4 GB，数据是 390.3 MB。

    那是同一个量换算之后取整，是正确的引用——不比换算就会冤枉它。
    """
    assert untraceable("`.map` 预计 0.4 GB。", ["390.3 MB 2164 个"]) == []


def test_换单位也拦得住编造() -> None:
    """放宽换算容差不等于放过编造：5.1 GB 说成 8 GB 偏 57%。"""
    found = untraceable("target 约 8 GB。", ["src-tauri 5.1 GB"])
    assert found, "量级改动必须拦得住"


def test_千分位不影响比对() -> None:
    assert untraceable("共 483,832 个文件。", ["合计：483832 个文件"]) == []


def test_渲染结果列出可疑项() -> None:
    text = render(untraceable("约 8 GB。", ["5.1 GB"]))
    assert "找不到出处" in text
    assert "8 GB" in text


def test_全部有出处时给的是肯定结论() -> None:
    text = render(untraceable("5.1 GB。", ["src-tauri 5.1 GB"]))
    assert "都能在工具输出里找到出处" in text
