"""autonomous 模式自动提权 policy=auto 的测试。

dogfood 暴露的问题：--autonomous 不带 --policy 时沿用工作区默认（通常是 ask），
无人值守写操作全被拒、白跑一趟。修复：autonomous + 空 policy 自动提权 auto。
"""
import argparse
from pathlib import Path
from unittest.mock import patch

from spoolkit.cli.commands.run import run


def _base_args(tmp_path: Path, **kwargs) -> argparse.Namespace:
    base = dict(
        root=str(tmp_path),
        provider="fake",
        model="",
        base_url="",
        proxy="",
        script="",
        window=4096,
        max_steps=2,
        step_ceiling=0,
        no_supervise=False,
        subagent_steps=2,
        no_memory=True,
        no_mcp=True,
        no_setup=True,
        delegate=False,
        plan=False,
        autonomous=True,
        resume=False,
        goal="测试任务",
        scope="**",
        cover="",
        limit=10,
        batch=2,
        no_roll=False,
        policy="",  # 关键：不指定 policy
        allow_read=[],
        events=False,
        from_channel=False,
        ask_on_stdin=False,
        web=True,
        web_allow=[],
        web_deny=[],
        web_search_api="",
        history=0,
        no_history=False,
        session="test-auto-policy",
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_autonomous_空policy自动提权auto(tmp_path: Path, capsys) -> None:
    """autonomous + policy='' → 自动提权为 auto，并打日志。"""
    args = _base_args(tmp_path)
    captured = {}

    def fake_autonomous(args, root, gateway):
        captured["policy"] = args.policy
        return 0

    with patch("spoolkit.cli.commands.run.autonomous", side_effect=fake_autonomous), \
         patch("spoolkit.cli.commands.run.provider_gateway", return_value=object()):
        rc = run(args)

    assert rc == 0
    assert captured["policy"] == "auto"
    out = capsys.readouterr().out
    assert "自动提权为 auto" in out


def test_autonomous_显式policy不覆盖(tmp_path: Path, capsys) -> None:
    """用户显式给了 --policy ask，不自动提权。"""
    args = _base_args(tmp_path, policy="ask")
    captured = {}

    def fake_autonomous(args, root, gateway):
        captured["policy"] = args.policy
        return 0

    with patch("spoolkit.cli.commands.run.autonomous", side_effect=fake_autonomous), \
         patch("spoolkit.cli.commands.run.provider_gateway", return_value=object()):
        run(args)

    assert captured["policy"] == "ask"
    out = capsys.readouterr().out
    assert "自动提权" not in out


def test_autonomous_events模式不打散文(tmp_path: Path, capsys) -> None:
    """events 模式下 stdout 只能有 JSON，提权日志不能打到 stdout。"""
    args = _base_args(tmp_path, events=True)
    captured = {}

    def fake_autonomous(args, root, gateway):
        captured["policy"] = args.policy
        return 0

    with patch("spoolkit.cli.commands.run.autonomous", side_effect=fake_autonomous), \
         patch("spoolkit.cli.commands.run.provider_gateway", return_value=object()):
        run(args)

    assert captured["policy"] == "auto"
    out = capsys.readouterr().out
    assert "自动提权" not in out  # events 模式不打散文到 stdout
