from pathlib import Path

from spoolkit.tools.edit import (
    PendingChanges,
    confirm_rewrite_spec,
    make_diff,
    replace_lines_spec,
    replace_text_spec,
    write_file_spec,
)
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall


def _pending(tmp_path: Path) -> PendingChanges:
    return PendingChanges(tmp_path)


def test_生成统一差异文本() -> None:
    diff = make_diff("a.py", "x = 1\n", "x = 2\n")
    assert "-x = 1" in diff
    assert "+x = 2" in diff
    assert "a/a.py" in diff


def test_写操作不落盘只产生diff(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "print(1)\n"}
    )
    assert result.ok is True
    assert "+print(1)" in result.content
    assert not (tmp_path / "new.py").exists()


def test_确认后写入磁盘(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "print(1)\n"}
    )
    written = pending.apply()
    assert written == ["new.py"]
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "print(1)\n"


def test_丢弃则不写入(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "print(1)\n"}
    )
    pending.discard()
    assert not (tmp_path / "new.py").exists()
    assert len(pending) == 0


def test_越界路径被拒绝(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "../outside.py", "content": "x\n"}
    )
    assert result.ok is False
    assert len(pending) == 0


def test_同一路径重复提出以最后一次为准(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    spec = write_file_spec(tmp_path, pending)
    spec.handler({"path": "a.py", "content": "第一版\n"})
    spec.handler({"path": "a.py", "content": "第二版\n"})
    assert len(pending) == 1
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "第二版\n"


def test_按行替换只改动指定区间(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 3, "content": "新行\n"}
    )
    assert result.ok is True
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\n新行\nl4\n"


def test_按行替换保留原有换行风格(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2", encoding="utf-8")
    pending = _pending(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 2, "content": "x"}
    )
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\nx"


def test_行号越界时拒绝(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("l1\nl2\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 5, "end_line": 9, "content": "x"}
    )
    assert result.ok is False
    assert "行号区间非法" in result.content


def test_对不存在的文件按行替换会拒绝(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "nope.py", "start_line": 1, "end_line": 1, "content": "x"}
    )
    assert result.ok is False


def test_修改已有文件时diff显示原文与改后(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "new = 2\n"}
    )
    assert "-old = 1" in result.content
    assert "+new = 2" in result.content


def test_内容没变时不登记改动(tmp_path: Path) -> None:
    """空的 diff 也拿去问用户，等于教他一律点「应用」。"""
    (tmp_path / "a.py").write_text("same = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "same = 1\n"}
    )
    assert result.ok is True
    assert "没有产生改动" in result.content
    assert len(pending) == 0


def test_新建空文件仍然算改动(tmp_path: Path) -> None:
    """「内容为空」和「没有改动」是两回事，别把新建空文件吃掉。"""
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "empty.py", "content": ""}
    )
    assert result.ok is True
    assert len(pending) == 1
    pending.apply()
    assert (tmp_path / "empty.py").exists()


def test_改测试文件时会提醒(tmp_path: Path) -> None:
    """实测模型会把测试改成 assert True 然后宣布完成，白烧四步。"""
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "test_thing.py", "content": "def test_a():\n    assert True\n"}
    )
    assert result.ok is True
    assert "这是测试文件" in result.content


def test_改普通文件不提醒(tmp_path: Path) -> None:
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "thing.py", "content": "x = 1\n"}
    )
    assert "这是测试文件" not in result.content


def test_片段替换只改那一处(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def old_name(x):\n    return x\n", encoding="utf-8"
    )
    pending = _pending(tmp_path)
    result = replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "def old_name(x):", "new": "def new_name(x):"}
    )
    assert result.ok is True
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == (
        "def new_name(x):\n    return x\n"
    )


def test_片段找不到时拒绝(tmp_path: Path) -> None:
    """找不到多半是缩进或空格没对上，要让它回去看原文，而不是猜。"""
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "value = 2", "new": "value = 3"}
    )
    assert result.ok is False
    assert "找不到这段内容" in result.content
    assert len(pending) == 0


def test_片段不唯一时拒绝(tmp_path: Path) -> None:
    """不唯一就必须让它多带上下文——猜一处改错的代价比多问一句大得多。"""
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    pending = _pending(tmp_path)
    result = replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "x = 1", "new": "x = 2"}
    )
    assert result.ok is False
    assert "出现了 2 次" in result.content
    assert len(pending) == 0


def test_片段替换能删内容(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("keep\nremove\n", encoding="utf-8")
    pending = _pending(tmp_path)
    replace_text_spec(tmp_path, pending).handler(
        {"path": "a.py", "old": "remove\n", "new": ""}
    )
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "keep\n"


def test_write_file描述引导局部修改(tmp_path: Path) -> None:
    """dogfood 暴露：整文件重写会丢内容（CSS 单位全丢）。
    描述里要引导模型改已有文件时优先用 replace_text/replace_lines。"""
    pending = _pending(tmp_path)
    desc = write_file_spec(tmp_path, pending).description
    assert "replace_text" in desc
    assert "replace_lines" in desc
    # 要说清楚为什么：整文件重写容易丢内容
    assert "丢掉" in desc or "丢" in desc


def test_整文件重写行数大减时警告(tmp_path: Path) -> None:
    """dogfood 暴露：重写 index.html 时 CSS 单位全丢（100vh→100%）。
    行数大幅减少时提醒模型检查有没有丢内容。"""
    original = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\n"
    (tmp_path / "big.py").write_text(original, encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "big.py", "content": "x = 1\n"}  # 11 行 → 1 行
    )
    assert result.ok is True
    assert "从 11 行变成 1 行" in result.content
    assert "丢掉" in result.content


def test_行数小幅变化不警告(tmp_path: Path) -> None:
    """行数减少不到 30% 不警告——正常修改不该被噪音打扰。"""
    original = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\nline11\n"
    (tmp_path / "big.py").write_text(original, encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "big.py", "content": "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n"}  # 11→8，减少 27%
    )
    assert result.ok is True
    assert "丢掉" not in result.content


def test_新建文件不警告(tmp_path: Path) -> None:
    """新建文件没有"原有内容"可丢，不该警告。"""
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "new.py", "content": "x = 1\n"}
    )
    assert result.ok is True
    assert "丢掉" not in result.content


def test_整文件重写减半被硬拦截(tmp_path: Path) -> None:
    """dogfood 第二轮：175 行重写成 117 行，丢标题/overlay/重启按钮。
    新行数 < 旧行数 × 50% 且文件 > 30 行时直接拒绝，逼模型用 replace_text。"""
    original = "\n".join(f"line{i}" for i in range(50)) + "\n"
    (tmp_path / "big.py").write_text(original, encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "big.py", "content": "x = 1\ny = 2\n"}  # 50 行 → 2 行
    )
    assert result.ok is False
    assert "拒绝" in result.content
    assert "replace_text" in result.content or "replace_lines" in result.content


def test_整文件重写减半时pending不登记(tmp_path: Path) -> None:
    """硬拦截后 pending 里不应该有这个改动——防止绕过。"""
    original = "\n".join(f"line{i}" for i in range(50)) + "\n"
    (tmp_path / "big.py").write_text(original, encoding="utf-8")
    pending = _pending(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "big.py", "content": "x = 1\n"}
    )
    assert len(pending) == 0


def test_整文件重写减不到一半不拦截(tmp_path: Path) -> None:
    """减少不到 50% 不拦截——正常修改不该被挡。"""
    original = "\n".join(f"line{i}" for i in range(50)) + "\n"
    (tmp_path / "big.py").write_text(original, encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "big.py", "content": "\n".join(f"line{i}" for i in range(30)) + "\n"}
    )  # 50 → 30，减少 40%
    assert result.ok is True


def test_小文件减半不拦截(tmp_path: Path) -> None:
    """文件 < 30 行时不拦截——小文件重写风险低。"""
    original = "\n".join(f"line{i}" for i in range(20)) + "\n"
    (tmp_path / "small.py").write_text(original, encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "small.py", "content": "x = 1\n"}  # 20 → 1
    )
    assert result.ok is True  # 小文件放行


# ---- 大重写的声明式通道（「三道墙」之一） ---------------------------------


def _two_documents(path: Path) -> str:
    """造一份「两份文档首尾相接」的样本：同一段内容出现两次。

    形状照抄 dogfood 现场（snake.html 是两份 HTML 拼在一起），只是小一号：
    合并成单文档时行数会掉一半以上，而丢掉的名字必须能被工具算出来。
    每份 18 行是为了越过护栏的 30 行线。
    """
    block = [
        "<html>",
        "<head>",
        "<style>",
        ".snake { color: red; }",
        "#board { margin: 0; }",
        "body { background: black; }",
        "</style>",
        "</head>",
        "<body>",
        '<div id="board" onclick="draw()"></div>',
        "<script>",
        "const canvas = 1;",
        "function draw() { return canvas; }",
        "let score = 0;",
        "function restart() { score = 0; }",
        "canvas.addEventListener('keydown', restart);",
        "</script>",
        "</body>",
    ]
    assert len(block) == 18
    return "\n".join(block * 2) + "\n"


_MERGED = """<html>
<style>
.snake { color: red; }
</style>
<script>
const canvas = 1;
</script>
</html>
"""


def test_未声明大重写仍被硬拦_要点是减少与改用(tmp_path: Path) -> None:
    """护栏不许松开：没声明的大重写照旧拦，而且话术要点是「减少」与「改用」。"""
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": _MERGED}
    )
    assert result.ok is False
    assert "减少" in result.content
    assert "改用" in result.content
    assert "消失清单" in result.content
    assert len(pending) == 0, "被拦下的重写不得进待确认清单"


def test_消失清单里必须有丢掉的函数与id(tmp_path: Path) -> None:
    """钉要点：故意丢掉一个函数与一个 id，这两个名字必须出现在清单里。"""
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": _MERGED}
    )
    assert result.ok is False
    assert "function draw" in result.content, "丢掉的函数名没进清单"
    assert "id=board" in result.content, "丢掉的 id 没进清单"
    assert "onclick=" in result.content, "丢掉的事件处理器没进清单"
    assert "#board" in result.content, "丢掉的 CSS 选择器没进清单"


def test_声明并逐项确认后大重写放行(tmp_path: Path) -> None:
    """合法大改（合并/去重）走声明通道：给出原因 + 逐项确认清单即可落盘。"""
    from spoolkit.tools.edit import disappeared_items

    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    spec = write_file_spec(tmp_path, pending)
    refused = spec.handler({"path": "snake.html", "content": _MERGED})
    assert refused.ok is False
    # 清单从「新旧两份正文」里逐项取出（模型看到的也是这份），全部照抄回去。
    acked = [item.token for item in disappeared_items(_two_documents(tmp_path), _MERGED)]
    assert acked, "这份样本应当有消失项"
    result = spec.handler(
        {
            "path": "snake.html",
            "content": _MERGED,
            "rewrite_reason": "两份文档首尾相接，合并成单文档并去掉重复声明",
            "acked_disappeared": acked,
        }
    )
    assert result.ok is True
    assert len(pending) == 1, "已声明的重写应当进待确认清单"
    note = pending.items()[0].declared
    assert "合并成单文档" in note, "待确认清单上没有留下重写理由"
    assert "消失清单" in note


def test_已声明但清单未交代完不落盘且点名(tmp_path: Path) -> None:
    """声明不等于免责：清单里还有没交代的项就不落盘，而且要**点名**。

    与上一版的差别（这一轮改的）：原先「没凑齐」直接拒收整份正文，模型只能
    把 120 行正文再发一遍——输出预算就是这么爆的。现在正文被**收下暂存**
    （文件仍未落盘），返回一个短令牌，缺的只是清单。
    """
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    original = (tmp_path / "snake.html").read_text(encoding="utf-8")
    result = write_file_spec(tmp_path, pending).handler(
        {
            "path": "snake.html",
            "content": _MERGED,
            "rewrite_reason": "合并两份文档",
            "acked_disappeared": ["function draw"],  # 只交代了一项
        }
    )
    assert result.ok is True, "正文要收下（拒收正文=逼它重发正文）"
    assert len(pending) == 0, "清单没交代完之前，文件不得进待确认清单"
    assert (tmp_path / "snake.html").read_text(encoding="utf-8") == original, (
        "文件必须尚未落盘"
    )
    assert "尚未落盘" in result.content
    assert "暂存" in result.content
    assert "id=board" in result.content, "没交代的项要被点名"
    assert "d" in result.content, "清单要带稳定项 id"
    # 令牌确实拿到了，而且只在暂存里（没有落盘）。
    assert len(pending.staged_items()) == 1
    token = pending.staged_items()[0].token
    assert token in result.content



def test_合并类任务的出路是可照做的下一步(tmp_path: Path) -> None:
    """dogfood 的现场是「两份文档拼在一起」，出路不能只会说「用 replace_lines」。

    必须给出具体行号：删哪一段、怎么删。
    """
    from spoolkit.tools.edit import NEXT_STEP_MARKER

    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": _MERGED}
    )
    assert result.ok is False
    assert "逐行相同" in result.content, "没认出「同一段内容出现两次」这个形状"
    remedies = [
        line for line in result.content.splitlines() if line.startswith(NEXT_STEP_MARKER)
    ]
    assert remedies, "没有给出照做的下一步"
    assert any("replace_lines(" in line and "start_line=" in line for line in remedies)
    # 行号必须是真的：第一份 1–18 行，第二份 19–36 行。
    assert any("start_line=19" in line for line in remedies)
    assert any("rewrite_reason=" in line for line in remedies), (
        "没告诉它声明式通道怎么走"
    )


def test_消失清单按标识符判定_改名不算消失(tmp_path: Path) -> None:
    """`const canvas` 改成 `let canvas` 时报「消失」会逼它确认一件没发生的事。"""
    from spoolkit.tools.edit import disappeared_items

    old = "const canvas = 1;\nfunction draw() {}\n"
    new = "let canvas = 2;\n"
    items = [item.token for item in disappeared_items(old, new)]
    assert items == ["function draw"]


def test_替换工具也能走声明通道(tmp_path: Path) -> None:
    """出路话术点的是 replace_lines，那它自己就必须能声明，否则是死路。"""
    (tmp_path / "big.py").write_text(
        "\n".join(f"line{i}" for i in range(80)) + "\n", encoding="utf-8"
    )
    pending = _pending(tmp_path)
    spec = replace_lines_spec(tmp_path, pending)
    refused = spec.handler(
        {"path": "big.py", "start_line": 20, "end_line": 80, "content": ""}
    )
    assert refused.ok is False
    allowed = spec.handler(
        {
            "path": "big.py",
            "start_line": 20,
            "end_line": 80,
            "content": "",
            "rewrite_reason": "删掉重复的那一段",
        }
    )
    assert allowed.ok is True
    assert "删掉重复的那一段" in pending.items()[0].declared


def test_纯HTML工作区只给write_file时出路不提不存在的工具(tmp_path: Path) -> None:
    """dogfood 复跑实测：`is_small_project` 只看 *.py，所以纯 HTML 项目走的是
    「只有 write_file」那条路——而旧的拒绝话术让它去用 replace_lines，
    那是一个在这个工作区里**根本不存在的工具**。出路指到墙外比不给更糟。
    """
    from spoolkit.tools.edit import register_edit_tools

    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    register_edit_tools(registry, tmp_path, pending)
    # 确认入口跟着 write_file 一起注册：少了它，暂存下去的正文没人能确认。
    assert registry.names() == ("confirm_rewrite", "write_file")

    result = registry.invoke(
        ToolCall("write_file", {"path": "snake.html", "content": _MERGED})
    )
    assert result.ok is False
    # 出路里不得出现一把不存在的工具的**调用**；只允许说清「它没注册」。
    assert "replace_lines(" not in result.content
    assert "改成 replace" not in result.content
    assert "只有 write_file" in result.content
    assert "都没注册" in result.content
    # 声明通道仍然要在：这是这个工作区唯一走得通的路。
    assert "rewrite_reason" in result.content
    assert "acked_disappeared" in result.content


# ---- 「确认清单拆成独立小回合」这一轮的验收 ---------------------------------
#
# 现场（dogfood r4）：11 步 / 15 次调用 / 315.9s / fail / 文件零改动。
# 卡点不是护栏松不松，而是**输出预算夹击**：清单必须一次凑齐 → 第一次调用
# 必然被拒（清单是工具算出来的）→ 120 行正文发第二遍 → 2457 token 装不下。
# 下面这几条把新形状钉死，其中「确认那次调用没有携带正文」是核心。


def _declared_rewrite(tmp_path: Path):
    """把两份文档合并成一份：返回（pending, token, 清单项 id 列表）。"""
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {
            "path": "snake.html",
            "content": _MERGED,
            "rewrite_reason": "两份文档首尾相接，合并成单文档并去掉重复声明",
        }
    )
    assert result.ok is True
    record = pending.staged_items()[0]
    return pending, record.token, list(record.ids()), result


def test_第一次重写返回清单与令牌且文件尚未落盘(tmp_path: Path) -> None:
    """第一条：带 rewrite_reason 的第一次重写 → 收下正文、给清单+令牌、不落盘。"""
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    original = (tmp_path / "snake.html").read_text(encoding="utf-8")
    pending = _pending(tmp_path)

    result = write_file_spec(tmp_path, pending).handler(
        {
            "path": "snake.html",
            "content": _MERGED,
            "rewrite_reason": "合并两份文档",
        }
    )

    assert result.ok is True
    assert len(pending) == 0, "还没确认，不得进待确认清单"
    assert pending.staged_count() == 1
    assert (tmp_path / "snake.html").read_text(encoding="utf-8") == original, (
        "文件尚未落盘"
    )
    record = pending.staged_items()[0]
    assert record.token in result.content, "返回里必须带令牌"
    assert "尚未落盘" in result.content
    assert "function draw" in result.content and "id=board" in result.content
    # 项 id 就在返回里，模型才能只发 id。
    assert all(one in result.content for one in record.ids())
    assert "不要把正文再发一遍" in result.content, "关键指令必须在返回里"


def test_只发项id的确认能落盘且确认那次没有正文(tmp_path: Path) -> None:
    """核心用例：确认入口**只吃项 id**——那次调用里一个正文字符都没有。

    断言分两半，缺一不可：
    ① 确认调用的参数里没有 content（也没有任何长正文）；
    ② 确认之后落成待确认改动，`apply()` 真的把合并后的正文写进文件。
    """
    pending, token, ids, staged_result = _declared_rewrite(tmp_path)
    confirm = confirm_rewrite_spec(tmp_path, pending)

    args = {"token": token, "acked": ids}
    result = confirm.handler(args)

    # ① 确认那次调用没有携带正文：参数只有项 id，长度远小于正文。
    assert "content" not in args
    assert sum(len(str(value)) for value in args.values()) < 100
    assert len(_MERGED) > 60  # 正文确实不小，不然这条断言没有意义
    assert result.ok is True

    # ② 落成待确认改动 → 落盘写的是合并后的正文。
    assert len(pending) == 1
    assert pending.staged_count() == 0
    assert pending.apply() == ["snake.html"]
    written = (tmp_path / "snake.html").read_text(encoding="utf-8")
    assert written == _MERGED
    assert written.count("<html") == 1, "产出必须是单文档"


def test_未交代的项仍在时不落盘并点名(tmp_path: Path) -> None:
    """硬要求不松：两者都没给的项只要还在，就不落盘，而且要点名是哪几项。"""
    pending, token, ids, _ = _declared_rewrite(tmp_path)
    confirm = confirm_rewrite_spec(tmp_path, pending)
    original = (tmp_path / "snake.html").read_text(encoding="utf-8")

    result = confirm.handler({"token": token, "acked": ids[:-1]})

    assert result.ok is False
    assert len(pending) == 0, "未交代完不得进待确认清单"
    assert pending.staged_count() == 1, "暂存的正文不许因为一次失败就被丢掉"
    assert (tmp_path / "snake.html").read_text(encoding="utf-8") == original
    missing_id = ids[-1]
    assert missing_id in result.content, "要点名具体是哪一项"
    assert "不用重发正文" in result.content, "失败话术也要说清正文不用重发"
    assert token in result.content, "令牌仍有效，要说出来"


def test_有意丢弃带理由可落盘且留痕可见(tmp_path: Path) -> None:
    """第二种选择：声明有意丢弃（带理由）→ 可以落盘，而且报告里看得出是哪一项。"""
    pending, token, ids, _ = _declared_rewrite(tmp_path)
    confirm = confirm_rewrite_spec(tmp_path, pending)
    dropped_id = ids[-1]

    result = confirm.handler(
        {
            "token": token,
            "acked": ids[:-1],
            "dropped": [f"{dropped_id}：第二份文档整段是重复脚本，确实不要了"],
        }
    )

    assert result.ok is True
    assert len(pending) == 1
    declared = pending.items()[0].declared
    # 留痕要能看出「哪一项被有意丢掉」+「为什么」——两者都要在。
    assert "消失清单" in declared
    assert "有意丢弃" in declared
    assert "第二份文档整段是重复脚本" in declared
    assert "确认保留" in declared
    # 模型这一侧也要看得到（报告里那份 diff 预览带着 declared）。
    assert "有意丢弃" in result.content
    assert "第二份文档整段是重复脚本" in result.content
    # 落盘之后产出是单文档。
    pending.apply()
    assert (tmp_path / "snake.html").read_text(encoding="utf-8") == _MERGED


def test_丢弃不写理由不放行(tmp_path: Path) -> None:
    """没有理由的「丢弃」与「漏了没交代」是同一件事，放行等于把硬要求掏空。"""
    pending, token, ids, _ = _declared_rewrite(tmp_path)
    confirm = confirm_rewrite_spec(tmp_path, pending)

    for entry in (ids[-1], f"{ids[-1]}：", f"{ids[-1]}："):
        result = confirm.handler(
            {"token": token, "acked": ids[:-1], "dropped": [entry]}
        )
        assert result.ok is False, entry
        assert "没写理由" in result.content
    assert len(pending) == 0
    assert pending.staged_count() == 1


def test_令牌无效或过期给人话且不落盘(tmp_path: Path) -> None:
    """令牌失效两条路：没见过 / 过期。两种都要给人话，都不许静默当成功。"""
    import spoolkit.tools.edit as edit

    pending, token, ids, _ = _declared_rewrite(tmp_path)
    confirm = confirm_rewrite_spec(tmp_path, pending)

    unknown = confirm.handler({"token": "rw-000000", "acked": ids})
    assert unknown.ok is False
    assert "没有这个令牌" in unknown.content
    assert "重新发起" in unknown.content, "要说清怎么重新发起"
    assert token in unknown.content, "还有效的那份要说出来（不静默丢弃）"
    assert pending.staged_count() == 1

    # 时效：把时钟推过去（monkeypatch，不 sleep）。
    now = edit._now()
    original_now = edit._now
    try:
        edit._now = lambda: now + edit.limits.resolve("rewrite_stage_ttl_seconds")[0] + 1
        expired = confirm.handler({"token": token, "acked": ids})
    finally:
        edit._now = original_now

    assert expired.ok is False
    assert "已过期" in expired.content
    assert "重新发起" in expired.content
    assert len(pending) == 0, "过期的确认不得落盘"
    assert pending.staged_count() == 0, "过期即按时效清理（上界的一部分）"


def test_多给一个不在清单里的项id要说不认识(tmp_path: Path) -> None:
    """模型抄错 id 时不能装作没事：认不出就要说，而它不会让任何一项被交代掉。"""
    pending, token, ids, _ = _declared_rewrite(tmp_path)
    confirm = confirm_rewrite_spec(tmp_path, pending)

    result = confirm.handler({"token": token, "acked": ids + ["d99"]})
    # d99 认不出，但清单本身交代齐了 → 落盘，同时在返回里说明 d99 被忽略。
    assert result.ok is True
    assert "d99" in result.content
    assert "不在这次的清单里" in result.content


def test_暂存有上界_条数与单份字符(tmp_path: Path) -> None:
    """上界是**可覆盖旋钮**，而且是硬闸：不是静默丢，是说清为什么没收下。"""
    from spoolkit.tools.edit import StageLimitError

    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "limits.json").write_text(
        '{"rewrite_stage_limit": 1, "rewrite_stage_chars": 100000}',
        encoding="utf-8",
    )
    pending = _pending(tmp_path)  # 不传 overrides：走工作区保存的那一份
    assert pending._knob("rewrite_stage_limit") == 1.0

    for name in ("a.html", "b.html"):
        (tmp_path / name).write_text(_two_documents(tmp_path), encoding="utf-8")
    spec = write_file_spec(tmp_path, pending)
    first = spec.handler(
        {"path": "a.html", "content": _MERGED, "rewrite_reason": "合并两份文档"}
    )
    assert first.ok is True
    second = spec.handler(
        {"path": "b.html", "content": _MERGED, "rewrite_reason": "合并两份文档"}
    )
    assert second.ok is False
    assert "最多暂存" in second.content, "上界要说出是哪个旋钮、现在是几"
    assert "rewrite_stage_limit" in second.content
    assert pending.staged_count() == 1
    # 单份字符上界：直接调 stage 判（工具路径上要先过「行数砍半」，绕不开）。
    from spoolkit.tools.edit import PendingChange

    try:
        pending.stage(
            PendingChange(path="c.html", old_text="x\n", new_text="y" * 200001),
            (),
            "超长",
            True,
        )
    except StageLimitError as exc:
        assert "rewrite_stage_chars" in str(exc)
    else:  # pragma: no cover - 上界失效时这条会红
        raise AssertionError("单份字符上界没有生效")


def test_同一路径再提一次会取代旧暂存并说出来(tmp_path: Path) -> None:
    """同一路径上更晚的正文取代更早的暂存：取代这件事必须被说出来。"""
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    spec = write_file_spec(tmp_path, pending)
    spec.handler(
        {"path": "snake.html", "content": _MERGED, "rewrite_reason": "合并两份文档"}
    )
    old_token = pending.staged_items()[0].token

    second = spec.handler(
        {
            "path": "snake.html",
            "content": _MERGED.replace("const canvas = 1;", "const canvas = 2;"),
            "rewrite_reason": "合并两份文档（第二次）",
        }
    )
    assert second.ok is True
    assert pending.staged_count() == 1, "同一路径只留一份"
    assert old_token in second.content, "旧令牌被取代要说出来，不许静默丢"
    assert "已被这次取代" in second.content

    # 旧令牌现在取不到了：话术要说清它被取代，而不是「没见过」。
    confirm = confirm_rewrite_spec(tmp_path, pending)
    stale = confirm.handler({"token": old_token, "acked": []})
    assert stale.ok is False
    assert "没有这个令牌" in stale.content
    assert pending.staged_items()[0].token in stale.content, "还有效的那份要报出来"

