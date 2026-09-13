"""数字可追溯性：交付物里的数字要在工具输出里找得到出处。

测试数据取自一次真实运行——它交出的清理报告里有两处数字与自己的数据矛盾，
而且方向相反（一个往上编、一个往下压）。这道检查要能把它们挑出来，
同时不能误伤那些正确引用的数字。
"""

from spoolkit.check.numbers import (
    actionable,
    claims_in,
    mismatched,
    render,
    summarize,
    untraceable,
)
from spoolkit.tools.types import Fact

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


# --- 配错对象：要结构化事实才拦得住 ---


def _facts() -> list[Fact]:
    """dir_stats 交出来的结构化事实（真实运行里那几项）。

    `torch_cuda.dll` 那条来自「最大的文件」一节——报告里那个 1.2 GB
    就是从这儿搬过去的，所以它必须在这张表里，否则第二道关过不去。
    """
    gb = 1 << 30
    return [
        Fact("合计", 67.0 * gb, "B"),
        Fact("tauri_player", 8.4 * gb, "B"),
        Fact("src-tauri", 5.1 * gb, "B"),
        Fact("pyvideotrans", 9.1 * gb, "B"),
        Fact(".venv", 7.6 * gb, "B"),
        Fact("torch_cuda.dll", 1.2 * gb, "B"),
        Fact("合计", 483832.0, "个文件"),
    ]


def test_能挑出配错对象的数字() -> None:
    """报告写 target 约 8 GB，但那个位置的主语 src-tauri 是 5.1 GB。

    数字本身有出处（tauri_player 整体是 8.4），所以「存在性检查」放行——
    只有结构化事实才拦得住。
    """
    report = "`tauri_player\\src-tauri\\target\\` 约 8 GB，可 cargo clean 释放。"
    found = [claim.text for claim in mismatched(report, _facts())]
    assert any("8 GB" in item for item in found), found


def test_配错对象能挑出拿局部当整体() -> None:
    report = "`pyvideotrans\\.venv\\` 约 1.2 GB，可随时重建。"
    found = [claim.text for claim in mismatched(report, _facts())]
    assert any("1.2 GB" in item for item in found), found


def test_正确配对不会被误伤() -> None:
    report = (
        "tauri_player 整体 8.4 GB，其中 src-tauri 5.1 GB；"
        "pyvideotrans 的 .venv 是 7.6 GB。"
    )
    assert mismatched(report, _facts()) == []


def test_没提到已知对象时不判断() -> None:
    """没有对象可比对就谈不上配错——宁可不说，也不要乱说。"""
    assert mismatched("预计可释放 42 GB。", _facts()) == []


def test_哪边都对不上的数不报配错() -> None:
    """**这条是取舍，不是能力**：只满足「对不上对象」还不够。

    「`src-tauri` 能安全清理约 2 GB」——2 GB 对不上 src-tauri（5.1），
    也不在别处出现过。但那是模型的判断（清理一部分本来就不是整体大小），
    不是抄错了对象。加上「这个数在别处有家」这道关，才能既拦得住
    `8 GB`（8.4 是 tauri_player 的）又不误伤这种推算。
    """
    report = "`src-tauri` 约 2 GB。"
    assert mismatched(report, _facts()) == []


def test_最近的对象没有这一类的数就不判() -> None:
    """「文件个数」是**目录**的属性，不是文件的。

    实测：`ChosenEnergeticGirl_v10.safetensors`**（2.0 GB / 仅 1 个文件）
    ——最近的对象名是那个文件名，它没有「个文件」这一类的事实，
    于是这个数压根不该拿来比。退回去找更远的 `ComfyUI_models`，
    找出来的主语根本不是它。
    """
    report = "`ChosenEnergeticGirl_v10.safetensors`（2.0 GB / 仅 1 个文件）"
    facts = [*_facts(), Fact("ComfyUI_models", 2.0 * (1 << 30), "B"),
             Fact("ComfyUI_models", 1.0, "个文件")]
    assert mismatched(report, facts) == []


def test_尾巴盖不住整个名字() -> None:
    """`.safetensors` 是 `NoobAI-XL-v1.1.safetensors` 的尾巴，结束在同一处。

    选成尾巴的话，比的就是「所有 .safetensors 加起来」，于是每一处都成了
    配错——实测 24 处噪声里有一多半是这么来的。
    """
    report = "`NoobAI-XL-v1.1.safetensors`（6.6 GB）"
    facts = [
        Fact(".safetensors", 20.0 * (1 << 30), "B"),
        Fact("NoobAI-XL-v1.1.safetensors", 6.6 * (1 << 30), "B"),
    ]
    assert mismatched(report, facts) == []


def test_数字里的点不算对象名() -> None:
    """`.1` 会撞进 `13.1 GB` 里，而且位置靠后，会盖掉真正的主语。"""
    report = "`models`（13.1 GB / 仅 2 个文件）"
    facts = [Fact(".1", 1000.0, "个文件"), Fact("models", 2.0, "个文件")]
    assert mismatched(report, facts) == []


def test_清了能省多少不判配错() -> None:
    """「清了能省多少」是推算，不是「它有多大」——实测这类占了噪声的大头。"""
    report = "预估可释放：若整个 AnimaLoraStudio 的 models 目录清掉，约 15–17 GB。"
    facts = [*_facts(), Fact(".dll", 15.9 * (1 << 30), "B")]
    assert mismatched(report, facts) == []


def test_没有结构化事实时这一层不工作() -> None:
    """只有渲染文本时，只能做存在性检查。这条边界写下来免得误会。"""
    assert mismatched("`src-tauri` 约 8 GB。", []) == []


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


def test_渲染里被截断的能用结构化事实补上() -> None:
    """`dir_stats` 只列前 8 个子项，第 9 个不在文本里。

    报告引用了它——那是真数据，不该报「找不到出处」。实测这么误报过 19 处：
    报告最长的「不要动」一节，几乎整节都被标了一遍。
    """
    rendered = "D:\\workSpace\n合计：483913 个文件，67.0 GB"
    facts = [Fact("cursor_acp_remote", 200.1, "MB"),
             Fact("cursor_acp_remote", 9076.0, "个文件")]
    assert untraceable("`cursor_acp_remote`（200.1 MB / 9076 个文件）", [rendered]) != []
    assert untraceable(
        "`cursor_acp_remote`（200.1 MB / 9076 个文件）", [rendered], facts
    ) == []


def test_渲染结果列出可疑项() -> None:
    text = render(untraceable("实测 8 GB。", ["5.1 GB"]))
    assert "找不到出处" in text
    assert "8 GB" in text


def test_带约数的引用不逐条列() -> None:
    """阈值只有实测定得下来；在此之前，先别把「约数」和「引用」混成一堆。"""
    text = render(untraceable("约 8 GB。", ["5.1 GB"]))
    assert "8 GB（第" not in text
    assert "推算而不是测量" in text


def test_约束不算约数() -> None:
    """`约` 是单字，会误撞「约束」——那是这个项目的常用词，不能当约数看。"""
    text = render(untraceable("受上下文约束，8192 token 没读完。", ["4096 token"]))
    assert "8192 token（第" in text


def test_引用与推算分开列() -> None:
    """混在一起列，看的人会忽略整张单子——而两类的价值完全不同。"""
    text = render(
        untraceable(
            "实测占用 99 GB。\n预估可释放：约 7–8 GB。",
            ["5.1 GB"],
        )
    )
    assert "看起来是引用" in text
    assert "99 GB" in text
    assert "是推算而不是测量" in text
    # 推算的那几个不该被逐条列出来
    assert "7 GB（第" not in text


def test_预测的量也算推算() -> None:
    """「可释放 30 GB」量的是「做了这事能省多少」，现在是多少它没说。"""
    text = render(untraceable("- `xxx` 可安全释放 30 GB。", ["5.1 GB"]))
    assert "30 GB（第" not in text
    assert "推算而不是测量" in text


def test_全部有出处时给的是肯定结论() -> None:
    text = render(untraceable("5.1 GB。", ["src-tauri 5.1 GB"]))
    assert "都能在工具输出里找到出处" in text


def test_一行说法只报问题和数量() -> None:
    """网页那边只有一行——把 35 个数字糊上去，等于没说。"""
    claims = untraceable("实测占用 99 GB。\n预估可释放：约 7–8 GB。", ["5.1 GB"])
    text = summarize(claims)
    assert "找不到出处 1 处：99 GB" in text
    assert "推算 1 处" in text
    assert "7 GB" not in text
    assert actionable(claims)


def test_只有推算时不算问题() -> None:
    """推算是它该做的判断——报成问题就是上次那种噪声。"""
    claims = untraceable("预估可释放约 7 GB。", ["5.1 GB"])
    assert claims, "先确认它确实找不到出处"
    assert not actionable(claims)
