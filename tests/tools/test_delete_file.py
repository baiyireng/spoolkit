"""删除文件的守卫、台账与可回滚。

为什么有这把工具（实测，不是设想）：merge-html 那条探针首轮跑红——模型把
`pricing.html` 的内容并进了 `index.html`，却**只能建议用户自己去 `rm`**
（两次输出一字不差），而用户那句「以后我就看一个文件」落不了地。
缺的不是能力，是一只手。

四道守卫每一条都对应一种真实的坏法：

- 越界：删除走 `resolve_within`（只有工作区），不走 `resolve_readable`
  （含 `--allow-read` 授权的外部目录）——那些目录本来就只读，能删就等于把
  「读得到」偷换成「删得掉」。
- 目录：删目录是递归操作，一次调用能毁掉整个工作区。
- 无理由：没有理由的删除在报告里与误删长得一模一样。
- 不存在：要给人话，而且**它自己刚删过**这一种必须分开说——报「不存在」会
  让模型以为自己没删成，于是原样再来一次，白烧一步。
"""

from pathlib import Path

from spoolkit.tools.edit import (
    DELETE_REASON_PARAM,
    DELETE_TOOL,
    PendingChanges,
    delete_file_spec,
    load_baseline,
    revert,
    save_baseline,
    write_file_spec,
)
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall
from spoolkit.tools.view import WorkspaceView


def _spec(root: Path, pending: PendingChanges):
    return delete_file_spec(root, pending)


def _call(path: str, reason: str = "内容已并入 index.html"):
    return {"path": path, DELETE_REASON_PARAM: reason}


def test_删除登记成待确认改动且不落盘(tmp_path: Path) -> None:
    target = tmp_path / "pricing.html"
    target.write_text("<html>贵</html>\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)

    result = _spec(tmp_path, pending).handler(_call("pricing.html"))

    assert result.ok is True
    # 文件还在磁盘上：删除与写改动走同一条「先看 diff 再落盘」的路。
    assert target.is_file()
    change = pending.items()[0]
    assert change.path == "pricing.html"
    assert change.deleted is True
    assert change.new_text == ""
    # 删除前的全文留在待确认记录里——它就是回滚的退路。
    assert change.old_text == "<html>贵</html>\n"
    assert "删除文件——内容已并入 index.html" in change.declared
    assert result.content.count("--- a/pricing.html") == 1
    assert "+++ /dev/null" in result.content


def test_落盘后回滚把文件原样恢复(tmp_path: Path) -> None:
    """「可回滚」对删除同样成立：基线里存的是全文，不是「它不存在」。"""
    body = "<html>贵</html>\n共 3 行\n"
    (tmp_path / "pricing.html").write_text(body, encoding="utf-8")
    pending = PendingChanges(tmp_path)
    _spec(tmp_path, pending).handler(_call("pricing.html"))

    baseline_path = tmp_path / ".agent" / "last_change.json"
    save_baseline(baseline_path, pending.baseline())
    pending.apply()
    assert not (tmp_path / "pricing.html").exists()

    baseline = load_baseline(baseline_path)
    assert baseline is not None
    touched = revert(tmp_path, baseline)
    assert touched == ["pricing.html"]
    assert (tmp_path / "pricing.html").read_text(encoding="utf-8") == body


def test_删掉新建的文件回滚后仍然不在(tmp_path: Path) -> None:
    """「之前不存在」与「被删掉」在事后长得一样，回滚动作正好相反。

    两位（old_text / existed）分开记就是为了这条：新建的文件回滚之后
    不该被凭空造出来。
    """
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "new.html", "content": "<html>新</html>\n"}
    )
    # 新建的改动落盘后再删一次，模拟「这一轮里先建后删」。
    pending.apply()
    pending2 = PendingChanges(tmp_path)
    _spec(tmp_path, pending2).handler(_call("new.html", "刚建的那份不要了"))
    baseline = pending2.baseline()
    pending2.apply()
    assert not (tmp_path / "new.html").exists()

    revert(tmp_path, baseline)
    assert (tmp_path / "new.html").read_text(encoding="utf-8") == "<html>新</html>\n"


def test_同一轮里新建后删除回滚不复活它(tmp_path: Path) -> None:
    """这一轮里刚建、又刚删：回滚的正确结果是「它还是不存在」。

    基线记的是「提出改动之前」的状态，所以 old_text 是 None；而删除这一处
    记的 old_text 是它被建出来时的内容。两者都留着，回滚时才不会把一份
    **这一轮自己造出来的**内容当成「原来就有的」写回去。
    """
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "new.html", "content": "<html>新</html>\n"}
    )
    delete_file_spec(tmp_path, pending).handler(_call("new.html", "刚建的那份不要了"))
    baseline = pending.baseline()
    entries = {e.path: (e.old_text, e.existed) for e in baseline.entries}
    # 只登记了删除这一处（同一路径以最后一次为准），而它「之前不存在」。
    assert entries == {"new.html": (None, False)}
    # 被删掉的那份正文仍在待确认记录里（留档 + 人看 diff 都用它）。
    assert pending.items()[0].old_text == "<html>新</html>\n"
    pending.apply()
    assert not (tmp_path / "new.html").exists()
    revert(tmp_path, baseline)
    assert not (tmp_path / "new.html").exists()


def test_删除台账里的路径就是被删的那个文件(tmp_path: Path) -> None:
    """物理路径与台账路径必须同源。

    这条不是洁癖：`raw` 用来判存在/类型与读正文，`stored` 用来落盘与回滚。
    两者由**同一个解析器**（`resolve_within`）产出时，`disk_path(stored)`
    必然等于 `raw`；换成一个会把 `..` / 关联根按别的口径折叠的解析器，
    就会出现「删的是 A、记账记的是 B」——而那种偏差平时完全看不出来。
    """
    (tmp_path / "pkg").mkdir()
    target = tmp_path / "pricing.html"
    target.write_text("<html>贵</html>\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)

    result = _spec(tmp_path, pending).handler(_call("pkg/../pricing.html"))

    assert result.ok is True
    change = pending.items()[0]
    assert change.deleted is True
    assert pending.disk_path(change.path).resolve() == target.resolve()
    assert change.old_text == "<html>贵</html>\n"


def test_关联目录里的文件也删不掉(tmp_path: Path) -> None:
    """`--allow-read` 授权的是**读**：能读到不等于删得掉。

    删除走 `resolve_within`（只有工作区），不走 `resolve_readable`（含额外
    可读根）——这一条钉的正是这个区别。

    见证方式刻意做成「**只要被放行，就一定看得见**」：`disk_path` 与删除
    登记用的是同一个解析口径，所以不管实现把关联路径记成相对还是绝对，
    只要这一次真被登记了，`disk_path(...) == victim` 就会成立。只断言
    「被拒」是不够的——那种断言在实现放宽边界时可能仍然满足。
    """
    outside = tmp_path.parent / "别人的目录"
    outside.mkdir(exist_ok=True)
    victim = outside / "important.txt"
    victim.write_text("别删我\n", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    pending = PendingChanges(root, extra_roots=(outside,))

    result = _spec(root, pending).handler(_call(str(victim)))

    assert result.ok is False
    assert pending.items() == []
    assert not any(
        pending.disk_path(item.path).resolve() == victim.resolve()
        for item in pending.items()
    )
    assert victim.is_file(), "关联目录里的文件被删掉了"


def test_相对路径的越界写法同样删不掉(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "邻居.txt").write_text("x\n", encoding="utf-8")
    pending = PendingChanges(root)

    result = _spec(root, pending).handler(_call("../邻居.txt"))

    assert result.ok is False
    assert (tmp_path / "邻居.txt").is_file()


def test_目录不删并指出它下面有什么(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)

    result = _spec(tmp_path, pending).handler(_call("docs", "这些都不需要了"))

    assert result.ok is False
    assert "只删文件，不删目录" in result.content
    assert "a.md" in result.content
    assert (tmp_path / "docs").is_dir()
    assert pending.items() == []


def test_没有理由就不执行(tmp_path: Path) -> None:
    (tmp_path / "pricing.html").write_text("x\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)

    for raw in ({}, {"path": "pricing.html"}, _call("pricing.html", "   ")):
        result = _spec(tmp_path, pending).handler(raw)
        assert result.ok is False
        assert DELETE_REASON_PARAM in result.content
        assert "没有理由" in result.content
    assert pending.items() == []
    assert (tmp_path / "pricing.html").is_file()


def test_目标不存在时说人话(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)

    result = _spec(tmp_path, pending).handler(_call("没这个文件.html"))

    assert result.ok is False
    assert "不存在" in result.content
    assert "list_dir" in result.content


def test_它自己刚删过时不报成失败(tmp_path: Path) -> None:
    """这一点必须分开说：报「不存在」会让它以为自己没删成，原样再来一次。"""
    (tmp_path / "pricing.html").write_text("x\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    assert _spec(tmp_path, pending).handler(_call("pricing.html")).ok is True

    again = _spec(tmp_path, pending).handler(_call("pricing.html"))

    assert again.ok is True
    assert "已经删除" in again.content
    assert "不需要再做一次" in again.content
    assert len(pending.items()) == 1


def test_视图里删除之后就读不到了(tmp_path: Path) -> None:
    """模型看到的世界必须一致：删了就是删了，不能再读出旧内容。

    不这么做的话，`read_file` 会把旧内容端回来，而模型据此以为自己没删掉。
    """
    target = tmp_path / "pricing.html"
    target.write_text("旧内容\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    _spec(tmp_path, pending).handler(_call("pricing.html"))

    view = WorkspaceView(tmp_path, pending)
    assert view.exists(target) is False
    assert target.resolve() in view.deleted_paths()
    try:
        view.read_text(target)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - 读到了就是坏
        raise AssertionError("删除之后视图仍然读得到旧内容")


def test_试跑副本里删除同样生效(tmp_path: Path) -> None:
    """模型以为删了，试跑环境里就必须真的没有它。

    否则它跑测试时读到的是一份自己那边已经删掉的文件——测出来的结论与它
    手上的工作区无关，那比不试跑更误导。真实工作区全程不动。
    """
    from spoolkit.tools.trial import trial_workspace

    target = tmp_path / "pricing.html"
    target.write_text("<html>贵</html>\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    delete_file_spec(tmp_path, pending).handler(_call("pricing.html"))

    with trial_workspace(tmp_path, pending.items()) as copy:
        assert not (copy / "pricing.html").exists()
    # 真实工作区一动不动：改动还在待确认清单里。
    assert target.is_file()


def test_注册之后索引里看得见这把工具(tmp_path: Path) -> None:
    """工具索引是模型唯一的线索：注册了却不在索引里，等于没有。"""
    from spoolkit.tools.edit import register_edit_tools

    registry = ToolRegistry()
    register_edit_tools(registry, tmp_path, PendingChanges(tmp_path))
    assert DELETE_TOOL in registry.names()
    assert DELETE_TOOL in registry.describe()
    assert DELETE_REASON_PARAM in registry.describe()
    assert "删除" in registry.explain(DELETE_TOOL)

    # 参数校验也真的罩着它：少给理由、多给一个不存在的参数都要被拦下。
    no_reason = registry.invoke(ToolCall(DELETE_TOOL, {"path": "a.txt"}))
    assert no_reason.ok is False
    unknown = registry.invoke(
        ToolCall(DELETE_TOOL, {"path": "a.txt", DELETE_REASON_PARAM: "x", "force": True})
    )
    assert unknown.ok is False
    assert "未知参数" in unknown.content
