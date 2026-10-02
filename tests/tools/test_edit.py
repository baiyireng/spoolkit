from pathlib import Path

from spoolkit.tools.edit import (
    PendingChanges,
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


def test_已声明但清单未确认完不放行(tmp_path: Path) -> None:
    """声明不等于免责：清单里还有没确认的项就不落盘。"""
    (tmp_path / "snake.html").write_text(_two_documents(tmp_path), encoding="utf-8")
    pending = _pending(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {
            "path": "snake.html",
            "content": _MERGED,
            "rewrite_reason": "合并两份文档",
            "acked_disappeared": ["function draw"],  # 只确认了一项
        }
    )
    assert result.ok is False
    assert len(pending) == 0
    assert "没有确认" in result.content
    assert "id=board" in result.content, "未确认的项要被点名"


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
    assert registry.names() == ("write_file",)

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
