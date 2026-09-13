from pathlib import Path

from spoolkit.policy import (
    ASK,
    AUTO,
    DEFAULT_POLICY,
    DENY,
    POLICIES,
    describe,
    load_policy,
    policy_path,
    save_policy,
)


def test_默认策略是最保守的一档() -> None:
    assert DEFAULT_POLICY == ASK


def test_三档策略都有说明() -> None:
    for name in POLICIES:
        assert describe(name) != f"未知策略 {name}"


def test_未设置时返回默认(tmp_path: Path) -> None:
    assert load_policy(policy_path(tmp_path)) == ASK


def test_保存后可读回(tmp_path: Path) -> None:
    path = policy_path(tmp_path)
    save_policy(path, AUTO)
    assert load_policy(path) == AUTO


def test_非法策略拒绝保存(tmp_path: Path) -> None:
    try:
        save_policy(policy_path(tmp_path), "whatever")
    except ValueError:
        return
    raise AssertionError("应当拒绝未知策略")


def test_文件内容非法时退回默认(tmp_path: Path) -> None:
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_policy(path) == ASK


def test_保存的值不在可选范围时退回默认(tmp_path: Path) -> None:
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"policy": "yolo"}', encoding="utf-8")
    assert load_policy(path) == ASK


def test_只读策略的描述提到丢弃() -> None:
    assert "丢弃" in describe(DENY)

