"""`spool plan` 必须**真的把计划写下来**，而且写进这次解析出来的那条会话。

为什么单开一个文件：Task 5 评审的 Critical-1 —— `make_plan` 在落盘那一步引用了
一个不存在的 `ctx`（`NameError`），于是 `spool plan` 每次都崩在写文件之前，
`plan.json` 从来没被写下来。而"`spool plan` 排的计划，`spool run --plan` 找得到"
是计划正文写死的硬约束，也是这次改动（`--session` 默认值改空串）的验收面。

之所以一直没人发现：整个测试套件里**没有一条**用例真的跑过 `make_plan`
（`autonomous` / `advance_plan` / `_survey` 都有人测，就是落盘这一条没有）。
"""

import argparse
import json
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.kernel import pointers, sessions
from spoolkit.session_state import plan_path


def _plan(goal: str) -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "goal": goal,
                    "acceptance": "跑 pytest 通过",
                    "scope": ["calc.py"],
                    "executor": "self",
                    "note": "",
                }
            ]
        },
        ensure_ascii=False,
    )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    return root.resolve()


def _script(tmp_path: Path, answers: list[str]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return path


def _use_fake(monkeypatch, script: Path) -> None:
    """`plan` 子命令没有 `--script`（那是 `run` 的参数），走环境变量给假模型喂脚本。"""
    monkeypatch.setenv("SPOOLKIT_SCRIPT", str(script))
    monkeypatch.setenv("SPOOLKIT_PROVIDER", "fake")


def test_计划真的落盘且落在指针那条会话里(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _workspace(tmp_path)
    monkeypatch.chdir(root)
    made = sessions.create_session(root, title="排这条")
    pointers.set("cli", root, made)  # 显式给了 --session 才不写指针，这里手动摆好
    _use_fake(monkeypatch, _script(tmp_path, [_plan("改 calc.py")]))

    code = main(
        [
            "plan",
            "--goal",
            "把 calc.py 改好",
            "--limit",
            "1",
            "--root",
            str(root),
        ]
    )

    assert code == 0
    written = plan_path(root, made)
    assert written.is_file(), f"计划没落盘：{written}"
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["steps"][0]["goal"] == "改 calc.py"
    assert str(written) in capsys.readouterr().out


def test_落盘位置跟着会话走(tmp_path: Path, monkeypatch) -> None:
    """显式 `--session` 时写进那一条——两条命令（plan / run --plan）必须同一处。"""
    root = _workspace(tmp_path)
    monkeypatch.chdir(root)
    other = sessions.create_session(root, title="另一条")
    _use_fake(monkeypatch, _script(tmp_path, [_plan("改 calc.py")]))

    code = main(
        [
            "plan",
            "--goal",
            "把 calc.py 改好",
            "--limit",
            "1",
            "--session",
            other,
            "--root",
            str(root),
        ]
    )

    assert code == 0
    assert plan_path(root, other).is_file()
    # 显式给了就不该动指针（设计 §3.6）
    assert pointers.get("cli") is None


def test_run_plan_读得到刚排好的计划(tmp_path: Path, monkeypatch, capsys) -> None:
    """验收那句原话：`spool plan` 排的计划，`spool run --plan` 找得到。

    只验"读得到"：真执行一步要跑验收命令、也要模型，那是别的用例的地盘。
    """
    from spoolkit.agents.plan import load_plan

    root = _workspace(tmp_path)
    monkeypatch.chdir(root)
    made = sessions.create_session(root, title="排这条")
    pointers.set("cli", root, made)
    _use_fake(monkeypatch, _script(tmp_path, [_plan("改 calc.py")]))
    assert (
        main(
            [
                "plan",
                "--goal",
                "把 calc.py 改好",
                "--limit",
                "1",
                "--root",
                str(root),
            ]
        )
        == 0
    )

    plan = load_plan(plan_path(root, made))
    assert plan.steps[0].goal == "改 calc.py"


def test_计划命令的会话兜底仍是cli(tmp_path: Path, monkeypatch) -> None:
    """没有指针、也没给 --session 时，落回 `cli`——老计划、老检查点都接得上。"""
    root = _workspace(tmp_path)
    monkeypatch.chdir(root)
    _use_fake(monkeypatch, _script(tmp_path, [_plan("改 calc.py")]))

    assert (
        main(
            [
                "plan",
                "--goal",
                "把 calc.py 改好",
                "--limit",
                "1",
                "--root",
                str(root),
            ]
        )
        == 0
    )

    assert plan_path(root, "cli").is_file()
