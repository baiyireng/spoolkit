from pathlib import Path

from agents_dev.tools.exec import run_command_spec
from agents_dev.tools.grant import ALLOWED, ASK, DENIED, Grants, classify, key_of


def test_白名单内直接放行() -> None:
    assert classify(["pytest", "-q"])[0] == ALLOWED


def test_永久禁止的操作不可申请() -> None:
    for argv in (
        ["rm", "-rf", "x"],
        ["shutdown", "/s"],
        ["sudo", "apt", "install", "x"],
        ["curl", "http://x"],
    ):
        level, _ = classify(argv)
        assert level == DENIED, argv


def test_git破坏性操作被永久禁止() -> None:
    assert classify(["git", "reset", "--hard"])[0] == DENIED
    assert classify(["git", "push"])[0] == DENIED
    assert classify(["git", "status"])[0] == ALLOWED


def test_白名单外但不在禁止清单里属于可申请() -> None:
    level, reason = classify(["npm", "install"])
    assert level == ASK
    assert reason


def test_程序名带路径也能匹配禁止清单() -> None:
    assert classify([r"C:\Windows\System32\format.com", "C:"])[0] == DENIED


def test_授权键取程序名与子命令() -> None:
    assert key_of(["npm", "install", "x"]) == "npm install"
    assert key_of(["pytest"]) == "pytest"


def _spec(tmp_path: Path, approver=None, grants=None):
    return run_command_spec(tmp_path, None, approver, grants)


def test_用户本轮批准后可以执行(tmp_path: Path) -> None:
    grants = Grants()
    result = _spec(
        tmp_path, approver=lambda argv, reason: "session", grants=grants
    ).handler({"command": ["git", "add", "-A"], "timeout": 30})
    assert grants.allows(["git", "add"])
    assert "用户拒绝" not in result.content


def test_用户永久批准会落盘(tmp_path: Path) -> None:
    path = tmp_path / ".agent" / "grants.json"
    grants = Grants(path=path)
    _spec(tmp_path, approver=lambda argv, reason: "always", grants=grants).handler(
        {"command": ["git", "add", "-A"], "timeout": 30}
    )
    assert path.exists()
    assert Grants(path=path).allows(["git", "add"])


def test_用户拒绝时明确报告(tmp_path: Path) -> None:
    result = _spec(
        tmp_path, approver=lambda argv, reason: "deny", grants=Grants()
    ).handler({"command": ["git", "add", "-A"], "timeout": 30})
    assert result.ok is False
    assert "用户拒绝" in result.content


def test_已授权的命令不再询问(tmp_path: Path) -> None:
    asked: list = []

    def approver(argv, reason):
        asked.append(argv)
        return "session"

    spec = _spec(tmp_path, approver=approver, grants=Grants())
    spec.handler({"command": ["git", "add", "-A"], "timeout": 30})
    spec.handler({"command": ["git", "add", "-A"], "timeout": 30})
    assert len(asked) == 1


def test_永久禁止的命令不会触发询问(tmp_path: Path) -> None:
    asked: list = []

    def approver(argv, reason):
        asked.append(argv)
        return "always"

    result = _spec(tmp_path, approver=approver, grants=Grants()).handler(
        {"command": ["rm", "-rf", "x"]}
    )
    assert result.ok is False
    assert asked == []

