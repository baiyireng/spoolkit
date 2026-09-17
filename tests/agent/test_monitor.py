from spoolkit.agent.monitor import (
    ToolSight,
    inspect,
    name_cycle,
    normalize_query,
    path_oscillations,
    same_query_count,
)


def test_工具名短环_write_再_check() -> None:
    names = [
        "write_file",
        "run_command",
        "write_file",
        "run_command",
        "write_file",
        "run_command",
    ]
    assert name_cycle(names) == "write_file→run_command"


def test_参数不同但名字成环仍能抓到() -> None:
    """dogfood：node --check 的文件名每次都变，完整 signature 抓不到。"""
    names = ["write_file", "run_command", "write_file", "run_command"]
    assert "write_file" in name_cycle(names)


def test_正常推进不成环() -> None:
    assert name_cycle(["read_file", "write_file", "run_command"]) == ""


def test_同路径内容来回覆盖算振荡() -> None:
    sights = [
        ToolSight("write_file", path="index.html", content_hash="aaa"),
        ToolSight("write_file", path="index.html", content_hash="bbb"),
        ToolSight("write_file", path="index.html", content_hash="aaa"),
    ]
    assert path_oscillations(sights) >= 1


def test_同关键词再搜() -> None:
    sights = [
        ToolSight("search_code", query="贪吃蛇 键盘"),
        ToolSight("search_code", query="贪吃蛇   键盘"),
        ToolSight("search_code", query="贪吃蛇 键盘"),
    ]
    assert same_query_count(sights) >= 3
    assert normalize_query("贪吃蛇   键盘") == normalize_query("贪吃蛇 键盘")


def test_inspect汇总() -> None:
    sights = [
        ToolSight("write_file", path="a.html", content_hash="1"),
        ToolSight("run_command", query="node --check x.js"),
        ToolSight("write_file", path="a.html", content_hash="2"),
        ToolSight("run_command", query="node --check y.js"),
    ]
    report = inspect(sights)
    assert report.cycle
