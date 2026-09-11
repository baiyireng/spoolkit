from pathlib import Path

from agents_dev.agents.plan import out_of_scope, parse_plan, path_in_scope
from agents_dev.cli.approval import apply_with_audit, review_and_apply
from agents_dev.tools.edit import PendingChanges, write_file_spec


def test_目录前缀视为范围内() -> None:
    assert path_in_scope("src/pkg/mod.py", ["src/pkg"]) is True


def test_同名前缀但不同目录不算范围内() -> None:
    # "src/pkg2" 不该被 "src/pkg" 命中——前缀匹配必须按路径段切，不能按字符切
    assert path_in_scope("src/pkg2/mod.py", ["src/pkg"]) is False


def test_通配模式生效() -> None:
    assert path_in_scope("tests/a/test_x.py", ["tests/**"]) is True


def test_空范围不放行任何路径() -> None:
    assert path_in_scope("a.py", []) is False


def test_列出越界路径() -> None:
    blocked = out_of_scope(["src/a.py", "docs/b.md"], ["src"])
    assert blocked == ["docs/b.md"]


def test_范围随计划一起解析() -> None:
    import json

    raw = json.dumps(
        {
            "steps": [
                {
                    "goal": "改索引",
                    "acceptance": "测试通过",
                    "scope": ["src/agents_dev/index"],
                }
            ]
        },
        ensure_ascii=False,
    )
    plan = parse_plan(raw, "目标")
    assert plan.steps[0].scope == ("src/agents_dev/index",)


def test_范围可持久化(tmp_path: Path) -> None:
    import json

    from agents_dev.agents.plan import load_plan, plan_path, save_plan

    raw = json.dumps(
        {"steps": [{"goal": "g", "acceptance": "a", "scope": ["src"]}]},
        ensure_ascii=False,
    )
    plan = parse_plan(raw, "目标")
    save_plan(plan_path(tmp_path), plan)
    restored = load_plan(plan_path(tmp_path))
    assert restored is not None
    assert restored.steps[0].scope == ("src",)


def _stage(tmp_path: Path) -> PendingChanges:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "src/a.py", "content": "x = 1\n"}
    )
    return pending


def test_自动落盘仍然打印差异与记录基线(tmp_path: Path) -> None:
    from agents_dev.tools.edit import load_baseline

    pending = _stage(tmp_path)
    baseline_path = tmp_path / ".agent" / "last_change.json"
    written = apply_with_audit(pending, baseline_path=baseline_path)
    assert written == ["src/a.py"]
    assert (tmp_path / "src" / "a.py").exists()
    # 自动应用不等于没有退路
    assert load_baseline(baseline_path) is not None


def test_逐项确认仍然可用(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    applied, written = review_and_apply(pending, ask=lambda _: "y")
    assert applied is True
    assert written == ["src/a.py"]

