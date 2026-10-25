"""自动验证：改完自己跑一遍项目测试。

要求有两条，缺一条这个功能就是假的：
1. 测的必须是**改过之后**的代码（所以跑在试跑副本上）；
2. 没有测试的项目不要瞎跑，否则「找不到测试」会被当成失败反馈灌回去。
"""

from pathlib import Path

from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.verify import (
    condense_test_output,
    detect_test_command,
    exit_code,
    is_test_path,
    looks_environmental,
    make_verifier,
    mentions_workspace,
    scope_for_change,
)
from spoolkit.tools.types import ToolResult

FAILING = "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 2\n"


def _project(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(FAILING, encoding="utf-8")


def test_没有测试也没有node就不给验证器(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert detect_test_command(tmp_path) is None
    # 没有 pytest 但 node 在 PATH 里时，会返回惰性 JS 验证器（降级验证）
    # mock node 不在，确认此时返回 None
    from unittest.mock import patch
    with patch("spoolkit.tools.verify.shutil.which", return_value=None):
        assert make_verifier(tmp_path, PendingChanges(tmp_path)) is None


def test_有测试才给验证器(tmp_path: Path) -> None:
    _project(tmp_path)
    assert detect_test_command(tmp_path) is not None
    assert make_verifier(tmp_path, PendingChanges(tmp_path)) is not None


def test_tests目录也算有测试(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path) is not None


# --- 验证范围跟着改动走 -----------------------------------------------


def _two_tasks(tmp_path: Path) -> None:
    """一个工作区里两件事，各有各的测试：一件会过、一件没过。"""
    for name, value in (("13_case_insensitive", 2), ("14_inclusive_range", 1)):
        home = tmp_path / name
        home.mkdir()
        (home / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (home / "test_acceptance.py").write_text(
            f"import mod\n\n\ndef test_v():\n    assert mod.VALUE == {value}\n",
            encoding="utf-8",
        )


def test_范围取最近的带测试的那一层(tmp_path: Path) -> None:
    _two_tasks(tmp_path)
    assert scope_for_change(tmp_path, "13_case_insensitive/mod.py") == "13_case_insensitive"
    # 单项目工作区：根目录自己有测试 → 还是根
    (tmp_path / "test_root.py").write_text("def test_ok():\n    assert True\n")
    assert scope_for_change(tmp_path, "anything/deep/mod.py") == "."


def test_只验改动那一处_别人的失败不报给它(tmp_path: Path) -> None:
    """这条是这次改动的全部意义：改 A 时不该收到 B 的失败。

    实测在 50 题的铺盘里，自动验证跑的是整条命令，于是每改完一道就报
    「失败」（另外 41 道还没做），反馈与它刚做的事无关。
    """
    _two_tasks(tmp_path)
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)

    # 只改会过的那一件
    pending.propose("13_case_insensitive/mod.py", "VALUE = 2\n")
    result = verifier(["13_case_insensitive/mod.py"])
    assert result.ok is True, result.content
    assert "13_case_insensitive" in result.content

    # 只改不会过的那一件：报的是它自己的失败，而且指明范围
    pending.propose("14_inclusive_range/mod.py", "VALUE = 9\n")
    result = verifier(["14_inclusive_range/mod.py"])
    assert result.ok is False
    assert "14_inclusive_range" in result.content
    assert "13_case_insensitive" not in result.content


def test_一次改多处就逐处报(tmp_path: Path) -> None:
    """对得上因果：哪一处通过、哪一处没过，一眼看得出是哪一个猜错了。"""
    _two_tasks(tmp_path)
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)
    pending.propose("13_case_insensitive/mod.py", "VALUE = 2\n")
    pending.propose("14_inclusive_range/mod.py", "VALUE = 9\n")

    result = verifier(
        ["13_case_insensitive/mod.py", "14_inclusive_range/mod.py"]
    )
    assert result.ok is False
    assert "改了 2 处" in result.content
    assert "1 处通过" in result.content
    assert "14_inclusive_range" in result.content


def test_未改动时验证报告失败(tmp_path: Path) -> None:
    _project(tmp_path)
    verifier = make_verifier(tmp_path, PendingChanges(tmp_path))
    result = verifier()
    assert result.ok is False
    assert "assert" in result.content or "failed" in result.content.lower()


def test_验证跑的是改动后的代码(tmp_path: Path) -> None:
    """这条是这个功能的意义所在：不改动的话，验证只会告诉你本来就知道的事。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)

    assert verifier().ok is False
    pending.propose("mod.py", "VALUE = 2\n")
    assert verifier().ok is True


def test_验证不写真实文件(tmp_path: Path) -> None:
    """跑在副本上，真实工作区全程不动——失败要能重来。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("mod.py", "VALUE = 2\n")
    make_verifier(tmp_path, pending)()
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_改测试骗不过验证(tmp_path: Path) -> None:
    """模型实测会这么干：把断言改成 assert True，验证就「通过」了。

    验证若按模型改过的测试判定，测的就是它希望看到的结论，
    而不是代码的真实表现。
    """
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose(
        "test_mod.py",
        "import mod\n\n\ndef test_v():\n    assert True\n",
    )
    verifier = make_verifier(tmp_path, pending)
    result = verifier()
    assert result.ok is False


def test_改测试时报告里要说明(tmp_path: Path) -> None:
    """悄悄按原始测试判定，模型会以为自己改测试生效了。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("mod.py", "VALUE = 2\n")
    pending.propose(
        "test_mod.py",
        "import mod\n\n\ndef test_v():\n    assert True\n",
    )
    result = make_verifier(tmp_path, pending)()
    assert result.ok is True
    assert "原来的内容" in result.content


def test_测试路径识别() -> None:
    assert is_test_path("test_mod.py")
    assert is_test_path("mod_test.py")
    assert is_test_path("tests/unit/test_x.py")
    assert is_test_path("test/helpers.py")
    assert not is_test_path("mod.py")
    assert not is_test_path("src/testing.py")


def test_短摘要优先() -> None:
    """一行摘要里同时有文件、用例名和差异，是最能行动的形态。"""
    raw = (
        "退出码 1（失败）\n$ python -m pytest -q\n"
        ".F                                                                       [100%]\n"
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_chinese _________________________________\n"
        "\n"
        "    def test_chinese():\n"
        '>       assert greet("Ann", "zh") == "你好，Ann！"\n'
        "E       AssertionError: assert '你好, Ann!' == '你好，Ann！'\n"
        "\n"
        "test_acceptance.py:9: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED test_acceptance.py::test_chinese - AssertionError: assert '你好, Ann!' ==\n"
        "============================== 1 failed in 0.05s ==============================\n"
    )
    condensed = condense_test_output(raw)
    assert condensed.startswith("FAILED test_acceptance.py::test_chinese")
    assert "你好, Ann!" in condensed
    # 分隔线和进度点不该混进来
    assert "=====" not in condensed


def test_有失败时失败排在错误前面且报条数() -> None:
    """实测形态（2026-10-07，B 靶副本里的裸 `pytest -q`）：26 条收集 ERROR 在前、
    1 条 FAILED 在后。旧实现按行顺序线性拼到 360 字，那条 FAILED **整条消失**，
    模型于是照着 `tests/cli/...` 回答。所以：失败优先，省略的条数要说出来。"""
    errors = [
        f"ERROR tests/cli/test_x{index}.py - ImportError: cannot import name 'render_status'"
        for index in range(26)
    ]
    raw = (
        "退出码 2（失败）\n"
        + "\n".join(errors)
        + "\nFAILED tests/services/test_service_ledger.py::test_停掉之后台账里要写明已经停过 - AssertionError\n"
    )
    condensed = condense_test_output(raw)
    assert condensed.startswith(
        "FAILED tests/services/test_service_ledger.py::test_停掉之后台账里要写明已经停过"
    )
    assert "共 1 条失败、26 条错误" in condensed
    # 列了 1 条失败 + 2 条错误，所以省掉 24 条——省掉多少必须写出来
    assert "省略 24 条" in condensed
    assert len(condensed) <= 360


def test_错误很多时列前几条并报总数() -> None:
    raw = "退出码 2（失败）\n" + "\n".join(
        f"ERROR tests/cli/test_x{index}.py - ImportError" for index in range(26)
    )
    condensed = condense_test_output(raw)
    assert condensed.startswith("ERROR tests/cli/test_x0.py")
    assert "共 26 条错误" in condensed
    assert "省略 20 条" in condensed
    assert len(condensed) <= 360


def test_没有短摘要时退回报错行() -> None:
    raw = (
        "退出码 1（失败）\n$ python -m pytest -q\n"
        "==== ERRORS ====\n"
        "E   ModuleNotFoundError: No module named 'helpers'\n"
    )
    assert "ModuleNotFoundError" in condense_test_output(raw)


def test_崩溃输出也能给出点东西() -> None:
    raw = "退出码 2（失败）\n$ python -m pytest -q\npython: can't open file 'x'\n"
    assert "can't open file" in condense_test_output(raw)


def test_退出码能被取出来() -> None:
    assert exit_code("退出码 1（失败）\n$ pytest\n") == 1
    assert exit_code("退出码 0（成功）\n$ pytest\n") == 0
    assert exit_code("没有退出码的一行") is None


def test_退出码分不出测试没过与没跑成() -> None:
    """实测：收集阶段被权限错误打断时，pytest 返回的也是 1。

    曾经按「非 1 即环境问题」判过，那条判据根本不会触发——记在这里，
    免得再走一遍。
    """
    assert exit_code("退出码 1（失败）\n") == 1
    assert exit_code("退出码 5（失败）\n") == 5


def test_报错指向工作区外的算环境问题(tmp_path: Path) -> None:
    outside = (
        "退出码 1（失败）\n$ pytest\n"
        "ERROR tests/x.py::test_a - PermissionError: [WinError 5] "
        "拒绝访问。: 'C:\\Users\\someone\\AppData\\Local\\Temp\\pytest-of-x'\n"
    )
    assert looks_environmental(outside, tmp_path) is True


def test_报错指向工作区内的算代码问题(tmp_path: Path) -> None:
    inside = (
        "退出码 1（失败）\n$ pytest\n"
        f"{tmp_path}\\mod.py:1: in <module>\n"
        "E   ModuleNotFoundError: No module named 'helpers'\n"
    )
    assert looks_environmental(inside, tmp_path) is False


def test_没有环境特征时不算环境问题(tmp_path: Path) -> None:
    plain = "退出码 1（失败）\n$ pytest\nFAILED test_x.py::test_a - AssertionError\n"
    assert looks_environmental(plain, tmp_path) is False


def test_一个测试都没收集到算环境问题(tmp_path: Path) -> None:
    assert looks_environmental("退出码 5（失败）\n$ pytest\n", tmp_path) is True


def test_工作区路径识别(tmp_path: Path) -> None:
    assert mentions_workspace(f"see {tmp_path}\\a.py:3", tmp_path) is True
    assert mentions_workspace("see C:\\Windows\\Temp\\a.py:3", tmp_path) is False


# --- ① 验证 scope 要回退：导不进项目根就回退到根，并说明为什么 ----------------
#
# 实测（rename-cross-file 复跑）：范围收窄到 `tests/` 之后，项目测试靠
# `sys.path` 找包根，于是报的是 `ERROR tests/test_pipeline.py`——
# 那是一条**假警报**，而模型会照着它去「修」没坏的东西。


def _project_without_path_hack(tmp_path: Path) -> None:
    """tests/ 里的测试**不**自己插 sys.path：收窄后必须靠回退才导得进。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(
        "from pkg.mod import VALUE\n\n\ndef test_v():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )


def test_范围收窄后导不进项目根就回退到根(tmp_path: Path) -> None:
    """子目录范围先把项目根补进 PYTHONPATH，导得进就不必换判据。

    这是决策①的第一条路：收窄范围**导得进**是第一位的，报假 ERROR 会诱导
    模型去修没坏的东西。补路径还不行的走第二条路（回退到根），见下一个用例。
    """
    _project_without_path_hack(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("pkg/mod.py", "VALUE = 2\n")
    result = make_verifier(tmp_path, pending)(["pkg/mod.py"])
    assert result.ok is True, result.content
    # 没有回退：子目录里就跑通了，判据没被换掉。
    assert "范围回退" not in result.content


def test_补路径还不行就回退到根并说明原因(tmp_path: Path, monkeypatch) -> None:
    """回退这条路本身要能走通，而且**必须说明为什么**——不能静默换判据。

    做法是把第一次（子目录）那次跑成「收集期 ImportError」：那正是实测里
    `rename-cross-file` 的形状（`ERROR tests/test_pipeline.py`）。
    真实的项目根那次照样真跑。
    """
    import spoolkit.tools.verify as module

    _project_without_path_hack(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("pkg/mod.py", "VALUE = 2\n")

    real = module.run_once
    calls: list[str] = []

    def fake(root, argv, **kwargs):
        scope = kwargs.get("cwd", ".")
        calls.append(scope)
        if scope == "pkg":
            return ToolResult(
                ok=False,
                content=(
                    "退出码 2（失败）\n$ python -m pytest -q\n"
                    "ERROR tests/test_mod.py\n"
                    "E   ImportError: cannot import name 'VALUE' from 'pkg.mod'\n"
                ),
            )
        return real(root, argv, **kwargs)

    monkeypatch.setattr(module, "run_once", fake)
    # 范围固定成 pkg：真实的 `scope_for_change` 会一路收到根（`pkg/` 里没测试），
    # 而这个用例要验的正是「范围窄了之后回退」那条路。
    monkeypatch.setattr(module, "scopes_for_changes", lambda *a, **k: (["pkg"], 0))
    try:
        result = make_verifier(tmp_path, pending)(["pkg/mod.py"])
    finally:
        monkeypatch.undo()

    assert calls == ["pkg", "."], "必须在项目根重跑一次"
    assert result.ok is True, result.content
    assert "范围回退" in result.content
    assert "sys.path" in result.content


def test_测试自己插了sys_path时不回退(tmp_path: Path) -> None:
    """多项目工作区里那种独立测试目录不该被回退牵连。

    判据是机械的：测试文件里有 `sys.path` 就说明它自己找得到包根，
    再回退等于把**别人的失败**报给它——那正是收窄范围要避免的事。
    """
    from spoolkit.tools.verify import scope_fallback

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n",
        encoding="utf-8",
    )
    assert scope_fallback(tmp_path, "tests", "ERROR tests/test_x.py\nImportError: x") == ""


def test_只有像范围问题才回退(tmp_path: Path) -> None:
    """断言失败是真失败，不许触发回退——否则验证会凭空贵一倍。"""
    from spoolkit.tools.verify import scope_fallback

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert 1 == 2\n")
    plain_failure = "退出码 1（失败）\nFAILED tests/test_x.py::test_x - AssertionError\n"
    assert scope_fallback(tmp_path, "tests", plain_failure) == ""
    # 收集期 ERROR + 导入失败：这才回退，而且原因是人话。
    broken = "退出码 2（失败）\nERROR tests/test_x.py\nE   ImportError: cannot import name 'a'\n"
    reason = scope_fallback(tmp_path, "tests", broken)
    assert "回退到项目根" in reason and "sys.path" in reason


def test_独立测试根不回退(tmp_path: Path) -> None:
    """有 conftest.py 的目录自己就是测试根，回退会把别人的失败带进来。"""
    from spoolkit.tools.verify import scope_fallback

    (tmp_path / "13_case").mkdir()
    (tmp_path / "13_case" / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "13_case" / "test_x.py").write_text("def test_x():\n    assert 1 == 2\n")
    broken = "退出码 2（失败）\nERROR test_x.py\nE   ImportError: no module named 'x'\n"
    assert scope_fallback(tmp_path, "13_case", broken) == ""


# --- ② 改名只落了一半：那叫中间态，不叫代码失败 ------------------------------
#
# 实测（rename-cross-file 复跑）就是这样翻车的：先落新名字，测试文件还是旧内容，
# 自动验证如实报 `ERROR tests/test_pipeline.py`，模型把这个中间态当代码问题，
# 为了让测试过，删掉了 `render()` 的表头行。


def _rename_project(tmp_path: Path) -> None:
    """两层包 + 一个引用旧名的测试，形状与 rename-cross-file 一致。"""
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pipeline" / "fetch.py").write_text(
        "def get_rows(source):\n    return [source]\n", encoding="utf-8"
    )
    (tmp_path / "pipeline" / "report.py").write_text(
        "from pipeline.fetch import get_rows\n\n\n"
        "def render(source):\n    return get_rows(source)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pipeline.py").write_text(
        "from pipeline.fetch import get_rows\n\n\n"
        "def test_rows():\n    assert get_rows('a') == ['a']\n",
        encoding="utf-8",
    )


def test_待确认改动里的改名能被算出来(tmp_path: Path) -> None:
    from spoolkit.tools.verify import pending_renames

    _rename_project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose(
        "pipeline/fetch.py",
        (tmp_path / "pipeline" / "fetch.py")
        .read_text(encoding="utf-8")
        .replace("get_rows", "load_rows"),
    )
    assert pending_renames(pending) == {"get_rows": "load_rows"}
    # 没改名字的改动不算改名：判据宁可不报，也不乱报。
    pending.propose("pipeline/report.py", "# 什么都没改名\n")
    assert pending_renames(pending) == {"get_rows": "load_rows"}


def test_改名只落一半时报中间态而不是代码失败(tmp_path: Path) -> None:
    _rename_project(tmp_path)
    pending = PendingChanges(tmp_path)
    # 只落了 fetch.py（新名字），report / tests 还没跟上——这正是实测的中间态。
    pending.propose(
        "pipeline/fetch.py",
        (tmp_path / "pipeline" / "fetch.py")
        .read_text(encoding="utf-8")
        .replace("get_rows", "load_rows"),
    )
    result = make_verifier(tmp_path, pending)(["pipeline/fetch.py"])
    assert result.ok is False                      # 判定不变：测试确实没过
    assert "中间态" in result.content               # 但性质说清了
    assert "get_rows→load_rows" in result.content
    assert "不得为了让测试通过而删改业务逻辑" in result.content


def test_不是改名造成的失败不加那句中间态(tmp_path: Path) -> None:
    """判据要准：一次普通改坏不能被说成「中间态」，那等于放行。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("mod.py", "VALUE = 3\n")       # 测试要 2，这次真的改错了
    result = make_verifier(tmp_path, pending)()
    assert result.ok is False
    assert "中间态" not in result.content


# --- ②b 测试文件跟着改名一起改：这是**必须**的一步，不能按旧测试判定 ----------
#
# 实测（rename-cross-file 复跑第 1 次，21 步红到底）：改名任务里测试文件必然要改
# ——旧测试引用的名字已经被改掉了。而「按原始内容判定」这条护栏让验证永远绿不了，
# 于是模型只有两条路：改测试（被拦）或改坏业务代码（它删了 render 的表头行）。
# 所以这一条单独认出「测试文件确实跟着这次改名改了」，用模型那份再判一次。


def test_测试跟着改名改时验证能绿(tmp_path: Path) -> None:
    _rename_project(tmp_path)
    pending = PendingChanges(tmp_path)
    for rel in ("pipeline/fetch.py", "pipeline/report.py", "tests/test_pipeline.py"):
        text = (tmp_path / rel).read_text(encoding="utf-8")
        if "get_rows" in text:
            pending.propose(rel, text.replace("get_rows", "load_rows"))
    result = make_verifier(tmp_path, pending)(
        [change.path for change in pending.items()]
    )
    assert result.ok is True, result.content
    assert "改名已经铺完" in result.content
    # 说清这一份是按**模型改过的测试**判的，不是静默换判据。
    assert "测试文件也跟着改了" in result.content


def test_把断言改成assert_True仍然骗不过验证(tmp_path: Path) -> None:
    """「改断言凑绿」这条后门的守卫：改名通道只认名字，不认断言改动。

    注意**不能**同时把业务代码改对——那样原始断言本来就该过，测不到这条守卫。
    所以这里业务代码保持 `VALUE = 1`，只把断言改成 `assert True`：
    按原始内容判定时必须报失败。项目里另放一条会过的测试，免得
    「一个测试都没收到」（退出码 5）被当环境问题放过。
    """
    _project(tmp_path)
    (tmp_path / "test_another.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    pending = PendingChanges(tmp_path)
    pending.propose("test_mod.py", "import mod\n\n\ndef test_v():\n    assert True\n")
    result = make_verifier(tmp_path, pending)()
    assert result.ok is False, result.content
    # 只改测试、没改业务代码时是**普通失败**：不带「中间态」那句
    # （那句是给「改名没铺完」用的；把它套在改断言上等于给谎报盖章）。
    assert "中间态" not in result.content


def test_改写断言之外的东西也不走那条通道(tmp_path: Path) -> None:
    """判据要窄：测试里除了改名还顺手改了别的东西，就回到「按原始测试」判定。"""
    from spoolkit.tools.verify import adapted_to_rename

    _rename_project(tmp_path)
    pending = PendingChanges(tmp_path)
    old = (tmp_path / "tests" / "test_pipeline.py").read_text(encoding="utf-8")
    pending.propose(
        "tests/test_pipeline.py",
        old.replace("get_rows", "load_rows").replace("def test_rows", "def test_rows_v2"),
    )
    assert adapted_to_rename(pending, {"get_rows": "load_rows"}, "ImportError get_rows") is False


def test_环境问题与代码问题给出不同反馈(tmp_path: Path) -> None:
    """环境问题要明确说「不是你的代码」，否则模型会去查自己的改动。

    实测踩过：环境坏了导致 pytest 收集失败，反馈却说「测试失败」，
    模型于是拿着 PermissionError 去查自己的改动，白烧好几步。
    """
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 2\n", encoding="utf-8"
    )

    import spoolkit.tools.verify as module

    original = module.run_once
    pending = PendingChanges(tmp_path)

    module.run_once = lambda *a, **k: ToolResult(
        ok=False, content="退出码 1（失败）\n$ pytest\nFAILED test_mod.py::test_v - AssertionError\n"
    )
    try:
        result = make_verifier(tmp_path, pending)()
        assert "测试没有通过" in result.content
        assert "不要改测试" in result.content
    finally:
        module.run_once = original

    module.run_once = lambda *a, **k: ToolResult(
        ok=False,
        content=(
            "退出码 1（失败）\n$ pytest\n"
            "ERROR tests/t.py::test_a - PermissionError: [WinError 5] 拒绝访问。\n"
        ),
    )
    try:
        result = make_verifier(tmp_path, pending)()
        assert "没能跑起来" in result.content
        assert "不是你的代码造成的" in result.content
    finally:
        module.run_once = original


def test_环境坏了之后不再重跑(tmp_path: Path) -> None:
    """结论不会变，而每跑一次都要往上下文里再灌一遍同样的报错。

    实测一次运行里它被重报了 7 次——模型明知道不可行动，还是每次都收到。
    """
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 2\n", encoding="utf-8"
    )
    import spoolkit.tools.verify as module

    calls = []
    original = module.run_once

    def fake(*args, **kwargs):
        calls.append(1)
        return ToolResult(
            ok=False,
            content=(
                "退出码 1（失败）\n$ pytest\n"
                "ERROR tests/t.py::test_a - PermissionError: 拒绝访问。\n"
            ),
        )

    module.run_once = fake
    try:
        verify = make_verifier(tmp_path, PendingChanges(tmp_path))
        first = verify()
        assert "没能跑起来" in first.content
        second = verify()
        assert "依然是坏的" in second.content
        assert len(calls) == 1, "环境确认坏了之后不该再跑命令"
    finally:
        module.run_once = original
