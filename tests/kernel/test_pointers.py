"""客户端指向：某个入口"现在指着哪儿"。"""
from pathlib import Path

from spoolkit.kernel import pointers


def test_没设过就是没有(tmp_path: Path) -> None:
    assert pointers.get("cli") is None


def test_设了能读回来(tmp_path: Path) -> None:
    pointers.set("cli", tmp_path, "alpha")
    got = pointers.get("cli")
    assert got is not None
    assert got.workspace == tmp_path.resolve()
    assert got.session == "alpha"


def test_每个键互不影响(tmp_path: Path) -> None:
    pointers.set("cli", tmp_path / "a", "one")
    pointers.set("chat:qqbot:u1", tmp_path / "b", "two")
    assert pointers.get("cli").session == "one"
    assert pointers.get("chat:qqbot:u1").session == "two"


def test_清掉之后就没有了(tmp_path: Path) -> None:
    pointers.set("cli", tmp_path, "alpha")
    pointers.clear("cli")
    assert pointers.get("cli") is None


def test_文件坏了当没设过(tmp_path: Path) -> None:
    path = pointers.pointers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert pointers.get("cli") is None
    assert pointers.all_pointers() == {}
