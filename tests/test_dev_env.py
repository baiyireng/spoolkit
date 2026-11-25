"""环境体检、环境准备、以及"环境豁免"三条硬规矩的用例。

这一层最容易变成"没人信的装置"：豁免一旦没有约束，就会从"环境挡住的"滑向
"我不想修的"。所以三条规矩都钉死在用例里：能力回来即失效、非受限环境整体
失效、过期直接红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dev_env
import gate
import pytest_needs


# ------------------------------------------------------------ 体检与环境准备


def test_体检表覆盖关键能力() -> None:
    caps = dev_env.probe_capabilities()
    for name in (
        "workspace_write",
        "tmpdir_write",
        "venv_write",
        "tool_file_write",
        "python_pipe",
        "node_pipe",
        "process_kill",
    ):
        assert name in caps, f"体检缺条目：{name}"
        assert isinstance(caps[name]["ok"], bool)
        assert caps[name]["detail"]


def test_登记表与探测一一对应() -> None:
    """登记了没探测 / 探测了没登记，都是"表在骗人"的入口。"""
    assert set(dev_env.probe_capabilities()) == set(dev_env.CAPABILITIES)


def test_每条能力都必须写出替代做法() -> None:
    """**这条是这套装置不退化的关键**：只报"不可用"、不给"所以呢"，
    下一个读到的人还得从头摸一遍（本会话就是这么摸的）。"""
    for name, info in dev_env.CAPABILITIES.items():
        assert info.label.strip(), f"{name} 没写标签"
        assert info.workaround.strip(), f"{name} 没写受限下的替代做法"
        if info.needs_unconfined:
            assert info.escalate.strip(), f"{name} 标了没有受限替代，就必须写清提权要做什么"


def test_缺能力时体检表就地印出动作() -> None:
    caps = {name: {"ok": True, "detail": "可用"} for name in dev_env.CAPABILITIES}
    caps["venv_write"] = {"ok": False, "detail": "PermissionError"}
    table = dev_env.render(caps)
    assert "受限下这么做" in table
    assert dev_env.CAPABILITIES["venv_write"].workaround[:20] in table
    assert dev_env.CAPABILITIES["venv_write"].escalate[:12] in table
    # 可用的那些不该把表撑成一片噪声
    assert table.count("受限下这么做") == 1


def test_任一宿主能力缺失即判受限() -> None:
    def caps(**missing: bool) -> dict:
        names = ("tmpdir_write", "venv_write", "tool_file_write", "node_pipe", "process_kill")
        return {name: {"ok": name not in missing, "detail": "x"} for name in names}

    assert dev_env.is_sandboxed(caps()) is False
    assert dev_env.is_sandboxed(caps(process_kill=True)) is True
    assert dev_env.is_sandboxed(caps(node_pipe=True)) is True


def test_环境准备把缓存与临时物都指到仓内(tmp_path: Path) -> None:
    """受限会话里用户目录往往不可写：缓存必须能"就仓"。"""
    env = dev_env.test_env(hanging_log=str(tmp_path / "progress.log"))
    for key in ("npm_config_cache", "UV_CACHE_DIR", "UV_TOOL_DIR", "PIP_CACHE_DIR"):
        assert Path(env[key]).is_relative_to(dev_env.CACHE_ROOT), f"{key} 没在仓内"
    assert "-p pytest_needs" in env["PYTEST_ADDOPTS"]
    parts = env["PYTHONPATH"].split(";")
    assert str(dev_env.REPO / "src") in parts
    assert str(dev_env.REPO / "scripts") in parts
    # 临时根必须在仓库外（放进仓库里，解析类用例会"向上找到 .agent"）
    assert not Path(env["PYTEST_DEBUG_TEMPROOT"]).is_relative_to(dev_env.REPO)


# ------------------------------------------------------------ 豁免三条规矩


def _caps(process_kill_ok: bool, *, also_missing: str = "") -> dict:
    """造一份能力表：`also_missing` 用来保证"仍在受限环境里"。

    判定受限看的是宿主能力那几条；如果全都可用，那就**不是**受限环境，
    豁免会按"非受限失效"处理（另一条规矩）——所以测"能力回来即失效"
    时必须让环境仍然是受限的。
    """
    caps = {
        name: {"ok": True, "detail": "可用"}
        for name in ("tmpdir_write", "venv_write", "tool_file_write", "node_pipe", "process_kill")
    }
    caps["process_kill"] = {
        "ok": process_kill_ok,
        "detail": "taskkill 可用" if process_kill_ok else "taskkill 被拒",
    }
    if also_missing:
        caps[also_missing] = {"ok": False, "detail": "被拒"}
    return caps


def _write_entries(tmp_path: Path, monkeypatch, entries: list[dict]) -> None:
    path = tmp_path / "known-env-failures.json"
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gate, "ENV_FAILURES", path)


def _entry(**over: object) -> dict:
    base = {
        "file": "tests/cli/test_service_command.py",
        "requires": "process_kill",
        "evidence": "实测 taskkill 被拒",
        "expires": "2099-01-01",
    }
    return {**base, **over}


def test_能力缺失时豁免生效(tmp_path: Path, monkeypatch) -> None:
    _write_entries(tmp_path, monkeypatch, [_entry()])
    active, problems = gate.load_env_failures(_caps(process_kill_ok=False))
    assert len(active) == 1
    assert problems == []
    hit = gate._exempt_for("tests/cli/test_service_command.py::test_x", active)
    assert hit is not None and hit["requires"] == "process_kill"


def test_能力可用时豁免失效并报问题(tmp_path: Path, monkeypatch) -> None:
    """**这是这套机制的关键**：能力回来了还不绿，就是真回归，不许再豁免。

    注意环境仍然是受限的（另有能力缺失）——否则走的是"非受限整体失效"那条。
    """
    _write_entries(tmp_path, monkeypatch, [_entry()])
    active, problems = gate.load_env_failures(
        _caps(process_kill_ok=True, also_missing="node_pipe")
    )
    assert active == []
    assert any("豁免失效" in item for item in problems)


def test_非受限环境整份豁免失效(tmp_path: Path, monkeypatch) -> None:
    _write_entries(tmp_path, monkeypatch, [_entry()])
    monkeypatch.setattr(gate.dev_env, "is_sandboxed", lambda caps=None: False)
    active, problems = gate.load_env_failures(_caps(process_kill_ok=False))
    assert active == []
    assert any("不是受限环境" in item for item in problems)


def test_过期豁免让关口红(tmp_path: Path, monkeypatch) -> None:
    _write_entries(tmp_path, monkeypatch, [_entry(expires="2000-01-01")])
    active, problems = gate.load_env_failures(_caps(process_kill_ok=False))
    assert active == []
    assert any("过期" in item for item in problems)


def test_到期日写错也报出来(tmp_path: Path, monkeypatch) -> None:
    _write_entries(tmp_path, monkeypatch, [_entry(expires="下个月")])
    active, problems = gate.load_env_failures(_caps(process_kill_ok=False))
    assert active == []
    assert any("YYYY-MM-DD" in item for item in problems)


def test_精确id与文件前缀都认(tmp_path: Path, monkeypatch) -> None:
    _write_entries(
        tmp_path,
        monkeypatch,
        [_entry(id="tests/a.py::test_one"), _entry(file="tests/b.py")],
    )
    active, _ = gate.load_env_failures(_caps(process_kill_ok=False))
    assert gate._exempt_for("tests/a.py::test_one", active) is not None
    assert gate._exempt_for("tests/a.py::test_two", active) is None
    assert gate._exempt_for("tests/b.py::test_anything", active) is not None


# ------------------------------------------------------------ needs 标记


def test_needs注册了标记() -> None:
    lines: list[str] = []

    class _Config:
        def addinivalue_line(self, name: str, line: str) -> None:
            lines.append(f"{name}:{line}")

    pytest_needs.pytest_configure(_Config())
    assert any("needs(capability)" in line for line in lines)


def test_未登记的能力名按不可用处理(monkeypatch) -> None:
    """写错能力名的症状必须是"跳过并说清楚"，而不是静默当成可用。"""
    monkeypatch.setattr(pytest_needs, "_CACHE", {})
    ok, detail, workaround = pytest_needs._capability("根本没这个能力")
    assert ok is False
    assert "未知能力" in detail
    assert workaround == ""


@pytest.mark.needs("python_pipe")
def test_needs在能力可用时照常跑() -> None:
    """能力在 → 用例照常执行（这条用例本身就是证明）。"""
    assert True
