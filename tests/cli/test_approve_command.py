"""`spool approve` 与 `spool init`：任意位置起手的那两条命令。

用户真实问过的一句话：「我可以在任意位置执行 `spool bridge --approve <码>` 吗」。
当时的答案是**不能**——它读的是当前目录下的 `.agent/bridge-pairings.json`，
在别的地方敲只会报"没有这个配对码"，而那句话指不到病根。这些用例把
"任意位置"钉死。
"""

import json
from pathlib import Path

from spoolkit.bridge.pairing import Pairings
from spoolkit.cli.app import main


def _pairing_workspace(tmp_path: Path) -> tuple[Path, Pairings]:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    pairings = Pairings(root / ".agent" / "bridge-pairings.json")
    return root, pairings


def test_在别的目录也能批准配对码(tmp_path, monkeypatch, capsys) -> None:
    root, pairings = _pairing_workspace(tmp_path)
    code = pairings.ensure_code("openid-abc")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # 别处没有 .agent，只能靠登记表；先登记（真机上由 init 或上一次运行做）
    from spoolkit import workspace

    workspace.register(root)

    assert main(["approve", code]) == 0

    assert "已批准" in capsys.readouterr().out
    assert Pairings(root / ".agent" / "bridge-pairings.json").is_approved("openid-abc")


def test_批准写的是工作区里那份文件(tmp_path, monkeypatch) -> None:
    """批准要落到**工作区**的配对文件里，而不是新造一份到当前目录。"""
    root, pairings = _pairing_workspace(tmp_path)
    code = pairings.ensure_code("openid-xyz")
    monkeypatch.chdir(root)
    assert main(["approve", code, "--root", str(root)]) == 0
    payload = json.loads((root / ".agent" / "bridge-pairings.json").read_text("utf-8"))
    assert "openid-xyz" in payload["approved"]


def test_码不认识时报清楚看的是哪个文件(tmp_path, monkeypatch, capsys) -> None:
    root, _ = _pairing_workspace(tmp_path)
    monkeypatch.chdir(root)
    assert main(["approve", "ZZZZZZ"]) == 2
    captured = capsys.readouterr()
    assert "没有这个配对码" in captured.err
    # 病根常常是"看错了工作区"，所以必须把实际看的文件说出来
    assert str(root / ".agent" / "bridge-pairings.json") in captured.err


def test_init_建出工作区并登记(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    monkeypatch.chdir(target)
    assert main(["init"]) == 0
    assert (target / ".agent").is_dir()
    assert target.resolve() in [item for item in __import__(
        "spoolkit.workspace", fromlist=["known"]
    ).known()]
    assert "已初始化工作区" in capsys.readouterr().out


def test_init_幂等(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    monkeypatch.chdir(target)
    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["init"]) == 0
    assert "已经是工作区" in capsys.readouterr().out


def test_init_不往已有工作区里套一层(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    deep = root / "src"
    deep.mkdir()
    monkeypatch.chdir(deep)
    assert main(["init"]) == 0
    assert not (deep / ".agent").exists()
    assert "不再套一层" in capsys.readouterr().out


def test_init_把_agent_写进_gitignore(tmp_path, monkeypatch) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    (target / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    monkeypatch.chdir(target)
    assert main(["init"]) == 0
    text = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".agent/" in text
    # 已有的忽略项不能被弄丢
    assert "*.pyc" in text


def test_不带参数且不是工作区时非交互打帮助(tmp_path, monkeypatch, capsys) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()
    assert not (plain / ".agent").exists()


# --- Task 6：机器级的码（带着"哪条通道、哪个工作区"）----------------------------


def test_机器级的码在任何目录都能批(tmp_path, monkeypatch, capsys) -> None:
    """码里带着通道与工作区，所以批准不需要当前目录或 --root。"""
    from spoolkit.kernel import trust

    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    code = trust.ensure_code("qqbot", "openid-a", root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["approve", code]) == 0

    out = capsys.readouterr().out
    assert "已批准 openid-a" in out
    assert trust.may_use("qqbot", "openid-a", root) is True


def test_旧式的码要能点名工作区(tmp_path, monkeypatch, capsys) -> None:
    """旧配对文件没有工作区维度：登记表里有多个时**必须**点名，否则说不清。"""
    from spoolkit import workspace

    a, pairings_a = _pairing_workspace(tmp_path)
    b = tmp_path / "ws-b"
    (b / ".agent").mkdir(parents=True)
    workspace.register(a)
    workspace.register(b)
    code = pairings_a.ensure_code("openid-legacy")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["approve", code]) == 2
    err = capsys.readouterr().err
    assert "--ws" in err
    assert str(a) in err and str(b) in err  # 候选要列出来

    assert main(["approve", code, "--ws", "ws"]) == 0  # a 的目录名就是 ws
    assert pairings_a.is_approved("openid-legacy") is True


def test_LIST_同时列机器级与旧文件的码(tmp_path, monkeypatch, capsys) -> None:
    from spoolkit import workspace
    from spoolkit.kernel import trust

    root, pairings = _pairing_workspace(tmp_path)
    workspace.register(root)
    trust.grant("qqbot", "openid-a", [root], label="手批")
    legacy = pairings.ensure_code("openid-legacy")
    machine = trust.ensure_code("qqbot", "openid-b", root)
    capsys.readouterr()

    assert main(["approve", "--list"]) == 0

    out = capsys.readouterr().out
    assert "openid-a" in out  # 机器级放行的人
    assert machine in out  # 机器级的待批准码
    assert legacy in out  # 旧文件里的待批准码
