import argparse
from pathlib import Path

from agents_dev.cli.app import resolve_policy, resolve_scope
from agents_dev.policy import ASK, AUTO, policy_path, save_policy


def _args(scope: str = "", policy: str = "") -> argparse.Namespace:
    return argparse.Namespace(scope=scope, policy=policy)


def test_命令行范围优先于计划范围() -> None:
    scope = resolve_scope(_args(scope="src, tests"), ("plan_dir",))
    assert scope == ("src", "tests")


def test_计划范围优先于默认值() -> None:
    assert resolve_scope(_args(), ("scratch_lab/",)) == ("scratch_lab/",)


def test_非计划运行默认覆盖全项目() -> None:
    # 默认回退成空：策略是持久化的，把某一次的 auto 放大成
    # 「此后每一次都免确认改整个项目」不是用户的持续选择。
    assert resolve_scope(_args()) == ()


def test_计划步骤未声明范围时回退成空() -> None:
    # 空范围意味着不许自动落盘，会走逐项确认。
    # 把「没声明」当成「全都允许」是危险的默认值。
    assert resolve_scope(_args(), default=()) == ()


def test_计划步骤未声明范围时不会自动放行(tmp_path: Path) -> None:
    from agents_dev.agents.plan import path_in_scope

    scope = resolve_scope(_args(), default=())
    assert path_in_scope("anything.py", scope) is False


def test_范围参数会去掉空白项() -> None:
    assert resolve_scope(_args(scope="src, ,tests,")) == ("src", "tests")


def test_策略命令行优先于已保存(tmp_path: Path) -> None:
    save_policy(policy_path(tmp_path), ASK)
    assert resolve_policy(_args(policy=AUTO), tmp_path) == AUTO


def test_未指定策略时用已保存的(tmp_path: Path) -> None:
    save_policy(policy_path(tmp_path), AUTO)
    assert resolve_policy(_args(), tmp_path) == AUTO
