"""L2 行为回归的用例（设计稿 §5 L2 / §3.1 / §2.5）。

这一层是被一个具体现场逼出来的：

    `vague-optimize` 在某一次扰动里把 `path_counts` 改成 `Counter`，却把
    「首次出现次序」的登记写错了位置 → `top_paths` 少算，**耗时降到 0.04s
    「变快了」而结果坏了**，而自动验证 2/2 绿、最终验证绿。静态一致性（L1）
    看不出这件事——只有「同一条命令、同一份输入、改动前后各跑一次」才看得见。

所以用例钉的是这条链上的四件事：

1. **对照运行真的能证伪**：把一份结果的数字改坏（行为变了），L2.2 必须报
   逐字不一致且高严重度；不改时必须一个字都不报（零误报是同一条判据）。
2. **「优化类任务」的行为变化不算通过**：任务要的是「更快、结果不变」时，
   变快不能抵消行为变了（这正是 Counter 那个案例的判据）。
3. **拿不到对照时明说「查不了」**：项目没有入口 / 没有可用输入时不许静默
   通过，也不许把「没测」读成「通过」。
4. **L2.3 的接口快照**：公开符号被删/改签名、CLI `--help` 文本变化都要报，
   并接上既有的 T1。

用例尽量走**真子进程**（判据本身不许被 mock 掉）：`_spawn` 那一层只在
「要控制耗时」的用例里换假 runner，判据与比对逻辑仍是真的。
"""

from __future__ import annotations

import json
from pathlib import Path

from spoolkit.agent import review_behavior as RB
from spoolkit.agent.audit import HIGH, INFO
from spoolkit.agent.review_gate import EscalationGate
from spoolkit.tools.edit import PendingChange

GOOD = '''"""小工具：把输入文件里的数字加起来。"""

import sys


def total(path):
    """读文件，把每行的整数加起来。"""
    numbers = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            numbers.append(int(line))
    return sum(numbers)


def render(value):
    return f"合计 {value}"


def main(argv):
    if len(argv) < 2:
        print("用法：python tool.py <文件>", file=sys.stderr)
        return 2
    print(render(total(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''

README = """# tool

把一份每行一个整数的文件加起来。

```
python tool.py data/nums.txt
```
"""


def make_project(root: Path, tool: str = GOOD) -> Path:
    """一份最小项目：README 里写着入口命令，data/nums.txt 是输入样本。"""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(README, encoding="utf-8")
    (root / "tool.py").write_text(tool, encoding="utf-8")
    body = "\n".join(str(index) for index in range(1, 401))
    (root / "data/nums.txt").write_text(body + "\n", encoding="utf-8")
    return root


def change_to(tool: str, *, old: str = GOOD) -> PendingChange:
    return PendingChange(path="tool.py", old_text=old, new_text=tool)


# ---- L2.2 对照运行 -----------------------------------------------------------


def test_行为没变时一个字都不报(tmp_path: Path) -> None:
    """零误报：同一份内容的两棵树，输出必须逐字相同。"""
    root = make_project(tmp_path)
    report = RB.review(root, [change_to(GOOD)], goal="把这个脚本整理一下")

    assert report.checked, report.render()
    assert report.same is True
    assert report.changed is False
    assert report.findings == [], [item.render() for item in report.findings]


def test_输出改了就必须报逐字不一致(tmp_path: Path) -> None:
    """L2.2 的证伪点：把 `render` 的文案改掉 → 报出来（行为确实变了）。

    **严重度是提示档**：这次任务（「整理一下」）本来就要改行为，把它报成
    高严重度会去拦一次正确的完成声明。升级成高严重度的那一档是「优化类
    任务上的行为变化」，见下面那条用例。
    """
    root = make_project(tmp_path)
    broke = GOOD.replace('return f"合计 {value}"', 'return f"总计 {value}"')
    assert broke != GOOD
    report = RB.review(root, [change_to(broke)], goal="把这个脚本整理一下")

    kinds = [item.kind for item in report.findings]
    assert RB.BEHAVIOR_CHANGED_KIND in kinds, report.render()
    finding = next(item for item in report.findings if item.kind == RB.BEHAVIOR_CHANGED_KIND)
    assert finding.severity == INFO, finding.render()
    assert "逐字" in finding.summary
    assert report.same is False


def test_优化任务里变快了但结果坏了不算通过(tmp_path: Path) -> None:
    """Counter 那个案例的判据：**行为变了就不算优化通过**。"""
    root = make_project(tmp_path)
    # 「变快」的改法：只加前 100 行（结果当然少了），耗时也短了。
    faster_but_wrong = GOOD.replace(
        "    return sum(numbers)",
        "    return sum(numbers[:100])",
    )
    report = RB.review(
        root,
        [change_to(faster_but_wrong)],
        goal="这个脚本跑得太慢了，帮我弄快点，结果别改坏了",
    )

    assert report.optimization is True
    assert report.changed is True
    assert report.faster is False
    kinds = [item.kind for item in report.findings]
    assert RB.BEHAVIOR_CHANGED_KIND in kinds
    assert RB.PERFORMANCE_REGRESSED_KIND in kinds
    perf = next(
        item for item in report.findings if item.kind == RB.PERFORMANCE_REGRESSED_KIND
    )
    assert perf.severity == HIGH
    assert "再快也不算" in perf.summary or "优化通过" in perf.summary


def test_优化任务判据认得出提速类请求() -> None:
    assert RB.is_optimization_task("这个脚本太慢了，弄快点，结果别改坏")
    assert RB.is_optimization_task("优化一下性能")
    assert not RB.is_optimization_task("把 get_rows 改名成 load_rows")
    assert not RB.is_optimization_task("加一个按月汇总的命令")
    assert not RB.is_optimization_task("")


def test_优化任务里没测出耗时也算查不了而不是通过(tmp_path: Path) -> None:
    """耗时太短时**如实报「量不出来」**（INFO），既不判通过也不判失败。"""
    root = make_project(tmp_path)
    report = RB.review(
        root, [change_to(GOOD)], goal="这个脚本太慢了，帮我弄快点"
    )
    assert report.changed is False
    # 0.4 万以内的输入上这段代码是毫秒级：必须落进「查不了」那一档。
    notes = [item for item in report.findings if item.kind == RB.BEHAVIOR_NOT_CHECKED_KIND]
    assert len(notes) == 1, [item.render() for item in report.findings]
    assert notes[0].severity == INFO
    assert RB.CANNOT_CHECK in notes[0].summary


# ---- 「查不了」的三种形状 ----------------------------------------------------


def test_没有入口时明确说查不了(tmp_path: Path) -> None:
    """项目里没有 README 用法示例 → 不许静默通过。"""
    root = make_project(tmp_path)
    (root / "README.md").unlink()
    report = RB.review(root, [change_to(GOOD)], goal="整理一下")

    assert report.same is None
    assert report.checked is False
    assert report.findings == []
    assert RB.CANNOT_CHECK not in report.cannot_check  # 理由由报告那段统一加前缀
    assert "找不到可跑的对照" in report.render()


def test_入口点名的输入不在项目里时也算查不了(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "data/nums.txt").unlink()
    report = RB.review(root, [change_to(GOOD)], goal="整理一下")
    assert report.same is None
    assert "找不到可跑的对照" in report.render()


def test_退出码变了也算行为变化(tmp_path: Path) -> None:
    """「同一条命令的输出」包含退出码：改动后直接退出 3，必须报出来。

    这一条是口径用例：只比 stdout 会漏掉「程序崩了 / 提前退出」这类回归，
    而退出码是机械可读的那一半证据。
    """
    root = make_project(tmp_path)
    boom = GOOD.replace("    return sum(numbers)", "    raise SystemExit(3)")
    assert boom != GOOD
    report = RB.review(root, [change_to(boom)], goal="整理一下", timeout=30.0)

    assert report.changed is True, report.render()
    kinds = [item.kind for item in report.findings]
    assert RB.BEHAVIOR_CHANGED_KIND in kinds
    finding = next(item for item in report.findings if item.kind == RB.BEHAVIOR_CHANGED_KIND)
    assert "退出码 0 → 3" in finding.detail


def test_没有改动时返回没跑的读数(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    report = RB.review(root, [], goal="什么都不做")
    assert report.checked is False
    assert "没有可比的改动" in report.cannot_check


# ---- 入口解析（同一条命令、同一份输入）--------------------------------------


def test_入口命令取自_README_里最小那份现成输入(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "data/small.txt").write_text("1\n2\n3\n", encoding="utf-8")
    (root / "README.md").write_text(
        README.replace(
            "python tool.py data/nums.txt",
            "python tool.py data/nums.txt\npython tool.py data/small.txt",
        ),
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None
    assert entry.command == ("python", "tool.py", "data/small.txt")
    assert entry.input_path == "data/small.txt"
    assert entry.generator == ()


def test_没有生成器时不猜命令(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "README.md").write_text("# tool\n\n没有用法示例。\n", encoding="utf-8")
    assert RB.entry_point(root) is None


def test_造输入命令要用消费输入的那条当入口(tmp_path: Path) -> None:
    """README 里同时有「造输入」和「跑工具」时，入口是后者、输入是生成的那份。

    拿生成器当入口是**假的通过**：`python gen.py` 不读日志，两次输出当然一样。
    """
    root = make_project(tmp_path)
    (root / "gen.py").write_text(
        "import sys\n\ncount = int(sys.argv[1])\n"
        'print("\\n".join(str(i) for i in range(count)))\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# tool\n\n小样本：\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "线上那份要自己造：\n\n```\npython gen.py 5000 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None
    assert entry.command == ("python", "tool.py", "data/big.txt")
    assert entry.generator == ("gen.py", "5000", "data/big.txt")
    assert entry.declared_units == 5000


def test_模块入口的用法示例也认(tmp_path: Path) -> None:
    """`python -m pkg.cli <输入文件>` 同样是一条「同输入」的对照（探针 rename 那条）。"""
    root = make_project(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "pkg/cli.py").write_text(
        "import sys\n\nfrom tool import total\n\n"
        "if __name__ == '__main__':\n    print(total(sys.argv[1]))\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# tool\n\n```\npython -m pkg.cli data/nums.txt\n```\n", encoding="utf-8"
    )
    entry = RB.entry_point(root)
    assert entry is not None
    assert entry.command == ("python", "-m", "pkg.cli", "data/nums.txt")
    assert entry.input_path == "data/nums.txt"


def test_只有开关的用法示例不算入口(tmp_path: Path) -> None:
    """`python -m pkg.cli total` 没有输入文件 → 没有「同一份输入」可比，不认。"""
    root = make_project(tmp_path)
    (root / "README.md").write_text(
        "# tool\n\n```\npython -m tool.cli total\n```\n", encoding="utf-8"
    )
    assert RB.entry_point(root) is None


def test_入口脚本本身没有_main_时也能跑对照(tmp_path: Path) -> None:
    """入口脚本只要存在就认——判据是「README 声明的命令跑得通」，不是有 main。"""
    root = make_project(tmp_path)
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n", encoding="utf-8"
    )
    entry = RB.entry_point(root)
    assert entry is not None and entry.command[1] == "tool.py"


# ---- L2.3 接口快照 -----------------------------------------------------------


def test_公开符号被删要报高严重度() -> None:
    """`EscalationGate` 的公开符号快照（T1 的同一批证据）接到 L2 的读数上。"""
    gate = EscalationGate(Path("."))
    before = GOOD
    after = GOOD.replace("def render(value):", "def format_value(value):")
    gate.snapshot_symbols("tool.py", before)
    changed = gate.changed_public_symbols({"tool.py": after})
    assert any("render" in item for item in changed), changed


def test_接口变化会进_L2_的发现清单(tmp_path: Path) -> None:
    """公开符号的变化进读数（提示档），升级走 T1 那条既有通道。"""
    root = make_project(tmp_path)
    after = GOOD.replace("def render(value):", "def format_value(value):")
    report = RB.review(
        root,
        [change_to(after)],
        goal="整理一下",
        interface_changes=["tool.py:render"],
    )
    kinds = [item.kind for item in report.findings]
    assert RB.CLI_SURFACE_CHANGED_KIND in kinds, report.render()
    finding = next(
        item for item in report.findings if item.kind == RB.CLI_SURFACE_CHANGED_KIND
    )
    # **提示档**：T1 已经拿这同一批证据升级过一次；这里再报高严重度就是把
    # 同一处接口变化数成两条，并且会把改名任务多拦一次。
    assert finding.severity == INFO, finding.render()
    assert finding.escalate is False
    assert finding.path == "tool.py"


def test_render_带上判据名与命令() -> None:
    """收尾报告里那一行必须能认出厂：判据名 + 实际跑的命令 + 输入。

    这一条同时钉住**报告的格式**（有测试用字符串替换改报告）：改坏了
    `_behavior_report_section` 的渲染就没人再读得懂那行说的是哪条判据。
    """
    report = RB.BehaviorReport(
        same=True,
        changed=False,
        command="python tool.py data/nums.txt",
        input_path="data/nums.txt",
    )
    line = report.render()
    assert line.startswith("L2.2 对照运行：")
    assert "python tool.py data/nums.txt" in line
    assert "逐字相同" in line
    assert "查不了" not in line


def test_cli_help_文本变化要报(tmp_path: Path) -> None:
    """子命令被删掉这类「公开符号没变、用户接口变了」的变化。"""
    cli_before = (
        "import argparse\n\n\ndef main():\n"
        "    parser = argparse.ArgumentParser(prog='tool')\n"
        "    parser.add_argument('--mode', choices=['a', 'b'])\n"
        "    parser.parse_args()\n\n\n"
        "if __name__ == '__main__':\n    main()\n"
    )
    cli_after = cli_before.replace("choices=['a', 'b']", "choices=['a']")
    root = make_project(tmp_path)
    (root / "cli.py").write_text(cli_after, encoding="utf-8")
    changes = [PendingChange(path="cli.py", old_text=cli_before, new_text=cli_after)]

    files = RB.cli_help_candidates(root, ["cli.py"])
    assert files == ["cli.py"]
    same, findings = RB.compare_cli_help(root, changes, files, timeout=60.0)
    # 帮助文本里的 choices 变了 → 必须报；两边都跑得起来才有结论。
    assert same is False, (same, findings)
    assert findings and findings[0].severity == HIGH
    assert findings[0].kind == RB.CLI_SURFACE_CHANGED_KIND


def test_cli_help_一致时不报(tmp_path: Path) -> None:
    cli = (
        "import argparse\n\n\ndef main():\n"
        "    parser = argparse.ArgumentParser(prog='tool')\n"
        "    parser.add_argument('--mode')\n"
        "    parser.parse_args()\n\n\n"
        "if __name__ == '__main__':\n    main()\n"
    )
    root = make_project(tmp_path)
    (root / "cli.py").write_text(cli, encoding="utf-8")
    changes = [PendingChange(path="cli.py", old_text=cli, new_text=cli)]
    same, findings = RB.compare_cli_help(root, changes, ["cli.py"], timeout=60.0)
    assert same is True and findings == []


def test_不是_cli_的脚本不进_help_快照(tmp_path: Path) -> None:
    """没有 argv / argparse / `__main__` 的纯库文件没有「CLI 接口」可比。"""
    root = make_project(tmp_path)
    (root / "pure.py").write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8"
    )
    assert RB.cli_help_candidates(root, ["pure.py"]) == []
    # 正例：真正的入口脚本必须被认出来（否则这条判据是空的）。
    assert RB.cli_help_candidates(root, ["tool.py"]) == ["tool.py"]


# ---- 标定：向下找「老实现跑得完」的规模 --------------------------------------


def test_标定会把跑不完的规模降一半(tmp_path: Path) -> None:
    """README 声明的规模上老实现跑不完时，向下标定到一半（两边同规模）。"""
    root = make_project(tmp_path)
    (root / "gen.py").write_text(
        "import sys\n\ncount = int(sys.argv[1])\n"
        'print("\\n".join(str(i % 7) for i in range(count)))\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "```\npython gen.py 100000 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None and entry.declared_units == 100000

    units, why, note = RB.calibrate_scale(root, entry, timeout=60.0)
    # 两档都必须跑得完（这份工具是 O(n)），所以按声明规模原样使用。
    assert why == ""
    assert units == 100000
    assert note == ""


def test_标定撞上超时返回零并让上层说查不了(tmp_path: Path) -> None:
    """连最小那一档都跑不完时不标定：让上层按「查不了」如实报。"""
    root = make_project(tmp_path)
    (root / "gen.py").write_text(
        "import sys\n\ncount = int(sys.argv[1])\n"
        'print("\\n".join(str(i % 7) for i in range(count)))\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "```\npython gen.py 100000 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None

    def sleepy(root_, command, timeout, *, stdout_to=""):  # noqa: ANN001
        if "tool.py" in command:
            return RB.RunResult(code=-1, stdout="", stderr="", status="timeout")
        return RB._spawn(root_, command, timeout, stdout_to=stdout_to)

    units, why, _note = RB.calibrate_scale(
        root, entry, timeout=60.0, runner=sleepy
    )
    assert units == 0
    assert why == ""


def test_标定在短预算超时后会用整条预算再试一次(tmp_path: Path) -> None:
    """标定的短预算被负载顶掉时，最小那一档必须用整条对照的预算再试一次。

    现场（探针 set5 的 `vague-optimize-r0`）：标定的 20s 短预算不够，
    于是只剩「按 README 的 40000 原样跑」——那一条必然超时，整条对照退化成
    「查不了」，最有价值的那个探针恰好测不到。
    """
    root = make_project(tmp_path)
    (root / "gen.py").write_text(
        "import sys\n\ncount = int(sys.argv[1])\n"
        'print("\\n".join(str(i % 7) for i in range(count)))\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "```\npython gen.py 100000 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None and entry.declared_units == 100000

    seen: list[float] = []

    def flaky(root_, command, timeout, *, stdout_to=""):  # noqa: ANN001
        if "tool.py" not in command:
            return RB._spawn(root_, command, timeout, stdout_to=stdout_to)
        seen.append(timeout)
        if timeout < 60.0:
            return RB.RunResult(code=-1, stdout="", stderr="", status="timeout")
        return RB.RunResult(code=0, stdout="合计 1\n", stderr="", seconds=1.5)

    units, why, note = RB.calibrate_scale(root, entry, timeout=60.0, runner=flaky)
    assert why == ""
    assert units == 50000
    assert note  # 「向下标定到 50000」那句话
    # 短预算试了两档，最后那一档用的是整条对照的预算。
    assert seen[-1] == 60.0, seen


def test_生成器被改过时两边都用改动前那一版(tmp_path: Path) -> None:
    """「同一份输入」的保障：生成器被改了就钉回改动前那一版。

    现场（探针 set5 的 `vague-optimize-r1`）：模型顺手把 `gen_log.py` 也改了，
    两边于是各生成一份不同的输入，L2 报出「改动后退出码 1」——那不是行为回归，
    是**输入不同了**。判据要成立，输入就必须由同一段代码产生。
    """
    root = make_project(tmp_path)
    gen = "import sys\n\ncount = int(sys.argv[1])\nprint(count)\n"
    changed_gen = "import sys\n\ncount = int(sys.argv[1])\nprint(count * 2)\n"
    (root / "gen.py").write_text(gen, encoding="utf-8")
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "```\npython gen.py 5000 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None

    changes = [
        PendingChange(path="tool.py", old_text=GOOD, new_text=GOOD),
        PendingChange(path="gen.py", old_text=gen, new_text=changed_gen),
    ]
    assert RB._freeze_generator(entry, changes) == "gen.py"

    # 两份副本里生成器都必须回到改动前那一版（否则输入不同）。
    for changes_for_copy in (
        RB._baseline_changes(changes),
        list(changes),
    ):
        trial = RB._Trial(root, changes_for_copy, "freeze-check")
        try:
            RB._pin_generator(trial.path, changes_for_copy, "gen.py")
            assert (trial.path / "gen.py").read_text(encoding="utf-8") == gen
        finally:
            trial.close()


def test_生成器被改过时对照仍用改动前那一版跑(tmp_path: Path) -> None:
    """走完整 `review()`：生成器被改，两边的输入仍必须由**同一段代码**产生。

    判据的形状是量出来的：把「行为」放进生成器产出的数据里（被改过的
    生成器写 `total=1`），于是「改动后那棵树跑的是改过的生成器」会立刻
    表现为输出不同——而那不是行为回归，是**输入不同**。所以这条用例既能
    证伪「钉回改动前那一版」这件事，也说明它为什么必须是判据的一部分。
    """
    root = make_project(tmp_path)
    gen = "print('total=3')\n"
    changed_gen = "print('total=999')\n"
    (root / "gen.py").write_text(changed_gen, encoding="utf-8")
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "```\npython gen.py 1 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None and entry.generator[0] == "gen.py"

    report = RB.review(
        root,
        [
            PendingChange(path="tool.py", old_text=GOOD, new_text=GOOD),
            PendingChange(path="gen.py", old_text=gen, new_text=changed_gen),
        ],
        goal="整理一下",
    )
    # 两边都跑改动前的生成器 → 输出一致；若改动后那棵树用了改过的生成器，
    # 这里会是「逐字不一致」。
    assert report.same is True, report.render()
    assert report.changed is False


def test_生成器没改过时不动它(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "gen.py").write_text("print(1)\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# tool\n\n```\npython tool.py data/nums.txt\n```\n\n"
        "```\npython gen.py 5000 > data/big.txt\n```\n",
        encoding="utf-8",
    )
    entry = RB.entry_point(root)
    assert entry is not None
    changes = [PendingChange(path="tool.py", old_text=GOOD, new_text=GOOD)]
    assert RB._freeze_generator(entry, changes) == ""


# ---- 与闸门的接线 ------------------------------------------------------------


def test_提示档的_L2_读数不拦完成声明(tmp_path: Path) -> None:
    """只有**高严重度**的 L2 发现才接进 T8；提示档只是读数。"""
    from spoolkit.agent.audit import Finding
    from spoolkit.tools.edit import PendingChanges

    root = make_project(tmp_path)
    loop = _loop(root, [_turn("交卷", final="好了")], PendingChanges(root))
    loop.audit_log.add(
        Finding(
            kind=RB.BEHAVIOR_CHANGED_KIND,
            severity=INFO,
            summary="行为变化（提示档）",
        )
    )
    assert loop._behavior_regression() is False
    loop.audit_log.add(
        Finding(
            kind=RB.PERFORMANCE_REGRESSED_KIND,
            severity=HIGH,
            summary="优化任务不通过",
        )
    )
    assert loop._behavior_regression() is True


def test_行为回归会被判成不许宣称完成(tmp_path: Path) -> None:
    """L2 的高严重度发现与 L1 同等对待：接在 T8/T12 上（§3.1）。"""
    from spoolkit.agent.audit import Finding

    gate = EscalationGate(tmp_path)
    finding = Finding(
        kind=RB.BEHAVIOR_CHANGED_KIND,
        severity=HIGH,
        summary="行为回归",
        detail="命令输出逐字不一致",
    )
    requests = gate.completion_triggers(
        [finding],
        files=["tool.py"],
        regression=True,
        blocking_failure=True,
        has_baseline=True,
        revert_command="spool revert --run r1",
        verified=True,
    )
    triggers = {item.trigger for item in requests}
    assert "T8" in triggers
    assert "T12" in triggers


def test_事件流里的_kind_与判据名一致(tmp_path: Path) -> None:
    """探针按 kind 数发现，所以两处的名字必须逐字一致。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import probe as probe_runner  # noqa: PLC0415

    for kind in (
        RB.BEHAVIOR_CHANGED_KIND,
        RB.CLI_SURFACE_CHANGED_KIND,
        RB.PERFORMANCE_REGRESSED_KIND,
    ):
        assert kind in probe_runner.REVIEW_NOTE_KINDS, kind
    assert RB.BEHAVIOR_NOT_CHECKED_KIND not in probe_runner.REVIEW_NOTE_KINDS


def test_收尾报告那一段带反向条款() -> None:
    """§2.5 的硬规矩：模型可见文本必须带反向条款，且不得读成「禁止删除」。"""
    import spoolkit.agent.loop as loop_module

    body = Path(loop_module.__file__).read_text(encoding="utf-8")
    assert "拦的是「完成声明」与危险动作，**不是成果**" in body
    assert "删死代码、合并重复实现、删已被替代的文件都是正常编辑" in body
    # 措辞纪律：不许出现「禁止删除」这类读法。
    assert "禁止删除" not in body


# ---- 与循环的接线（收尾报告那一段）------------------------------------------


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _loop(root: Path, script: list, pending, events: list | None = None):
    from spoolkit.agent.loop import AgentLoop
    from spoolkit.config import Config
    from spoolkit.llm.fake import FakeModel
    from spoolkit.llm.tokenizer import OfflineTokenCounter
    from spoolkit.tools.edit import register_edit_tools
    from spoolkit.tools.fs import list_dir_spec, read_file_spec
    from spoolkit.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(list_dir_spec(root, pending))
    registry.register(read_file_spec(root, pending))
    register_edit_tools(registry, root, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=root, context_window=8192, max_steps=6),
        pending=pending,
        # 事件流是可选的：要读「过程区」的用例把一份 list 传进来。
        on_event=(
            (lambda kind, data: events.append((kind, data)))
            if events is not None
            else None
        ),
    )


def test_L2_读数进事件流而结论报告不出现系统账目(tmp_path: Path) -> None:
    """政策重述：原 `test_收尾报告里有_L2_那一段且明确说查不了`（709–742 行）。

    **旧断言逐字是这两条**（改动前）：

        assert "L2 行为回归读数" in result.final
        assert "查不了" in result.final
        # §2.5 的反向条款必须一起进模型可见文本
        assert "不是成果" in result.final

    也就是说：「L2 那一段出现 → 报告里必须同时出现反向条款」。它与原则直接
    冲突：`risky-action` 那一轮**什么都没拦下**，只是 L2 打了读数（`输出逐字
    不一致`），报告里就照样多出一段 §2.5 反向条款 → 问题③复现。所以按新政策
    **重述**（这是政策驱动的重述，不是放宽）：

      - L2 读数（判据原话 / 实际跑的命令 / 逐字比对 / 「查不了」的理由）是
        **过程材料**：它在事件流与详情里可查，用来跟踪 agent 的思路与排查；
      - 结论报告里**不出现**系统账目原文；
      - §2.5 的反向条款**仅在被拦下时**出现（它是给「被审查拦下」那句判词配的）。

    下面第 ② 组是**正向断言**：读数必须真的能在事件流里找到——不许用
    「报告里看不到」冒充「通过」，那等于把真读数藏起来。
    """
    from spoolkit.tools.edit import PendingChanges

    root = make_project(tmp_path)
    (root / "README.md").write_text("# tool\n\n没有用法示例。\n", encoding="utf-8")
    pending = PendingChanges(root)
    events: list[tuple[str, dict]] = []
    loop = _loop(
        root,
        [
            _turn(
                "写一个文件",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "note.py", "content": "X = 1\n"},
                        "why": "先写一份产出，看收尾报告怎么记",
                    }
                ],
            ),
            _turn("交卷", final="写好了"),
        ],
        pending,
        events,
    )

    result = loop.run("写一份 note.py")

    # ① 结论报告（用户读的那一份）：没有系统账目原文，也没有反向条款
    #    ——这一轮什么都没拦下（账本里没有高严重度发现）。
    assert "L2 行为回归读数" not in result.final, result.final
    assert "【查不了】" not in result.final, result.final
    assert "不是成果" not in result.final, result.final
    # ② **读数仍可查**：它落在事件流那条过程 note 里（正向证据，缺了这条
    #    就等于把真读数藏了）。
    notes = [str(data.get("text") or "") for kind, data in events if kind == "note"]
    readings = [text for text in notes if "L2 行为回归读数" in text]
    assert readings, f"L2 读数在事件流里查不到：{notes}"
    assert "【查不了】" in readings[0], readings[0]
    assert any(
        str(data.get("kind") or "") == "behavior_readings"
        for kind, data in events
        if kind == "note"
    ), "事件流里的读数没有判据分类（kind）"
    assert loop._behavior_report is not None
    assert loop._behavior_report.checked is False
    # 拦的是结论与动作，成果照落盘。
    assert [item.path for item in pending.items()] == ["note.py"]


def test_循环把目标原文交给_L2(tmp_path: Path) -> None:
    """L2.4 要拿目标原文判「这次任务是不是优化类」——它必须真的传进去。

    这条是**实测漏掉过一次**的判据：`_goal_text` 原先写在 `run()` 里
    「这一轮状态重置」之前，被重置那行清零，于是优化类任务永远判不出来
    （探针里 `vague-optimize` 的 L2.4 整条没跑，报告只说「入口跑不完」）。
    """
    from spoolkit.tools.edit import PendingChanges

    root = make_project(tmp_path)
    pending = PendingChanges(root)
    loop = _loop(
        root,
        [
            _turn(
                "先写一份产出",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "note.py", "content": "X = 1\n"},
                        "why": "先写一份产出，看 L2 拿到目标没有",
                    }
                ],
            ),
            _turn("交卷", final="写好了"),
        ],
        pending,
    )

    goal = "这个脚本跑得太慢了，帮我弄快点，结果别改坏了"
    loop.run(goal)

    assert loop._goal_text == goal, "目标原文没进 L2（L2.4 的判据就永远不成立）"
    report = loop._behavior_report
    assert report is not None
    assert report.optimization is True


def test_发现能被序列化进事件流(tmp_path: Path) -> None:
    """收尾报告与事件流都要能原样带上这些发现（不依赖模型的总结文字）。"""
    root = make_project(tmp_path)
    broke = GOOD.replace('return f"合计 {value}"', 'return f"总计 {value}"')
    report = RB.review(root, [change_to(broke)], goal="整理一下")
    payload = json.dumps(
        [{"kind": item.kind, "severity": item.severity} for item in report.findings],
        ensure_ascii=False,
    )
    assert RB.BEHAVIOR_CHANGED_KIND in payload


def test_真的被拦下时反向条款照出而读数仍在事件流(tmp_path: Path) -> None:
    """新政策的两侧（与上面那条重述配套）：**拦下了** → 反向条款出现。

    上面那条钉的是「没拦下 → 反向条款一个字都不出」；这一条钉的是反面：
    优化类任务上行为变了（L2 报高严重度 → T12 拦下完成声明）时，报告里必须
    有 §2.5 的反向条款——它是给「被审查拦下」那句判词配的（否则会被读成
    「成果被扣住了」）。**读数不进报告**这条在两种情况下都成立。
    """
    from spoolkit.tools.edit import PendingChanges

    root = make_project(tmp_path)
    broke = GOOD.replace('return f"合计 {value}"', 'return f"总计 {value}"')
    assert broke != GOOD
    pending = PendingChanges(root)
    events: list[tuple[str, dict]] = []
    loop = _loop(
        root,
        [
            _turn(
                "换成另一种写法",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "tool.py", "content": broke},
                        "why": "试着改快一点",
                    }
                ],
            ),
            _turn("交卷", final="弄快了"),
        ],
        pending,
        events,
    )

    result = loop.run("这个脚本跑得太慢了，帮我弄快点，结果别改坏了")

    # 真的拦下了（L2 的高严重度发现接在 T12 上）。
    assert loop._review_blocked is True
    assert "被审查拦下" in result.final, result.final
    # 反向条款与用户语言那一句都在；系统账目原文（读数）仍然不在。
    assert "不是成果" in result.final, result.final
    assert "删死代码、合并重复实现、删已被替代的文件都是正常编辑" in result.final
    assert "这次改动会改变程序原有行为" in result.final, result.final
    assert "L2 行为回归读数" not in result.final, result.final
    # 读数照样可查：事件流那条过程 note 里带着逐字比对与命令。
    notes = [str(data.get("text") or "") for kind, data in events if kind == "note"]
    readings = [text for text in notes if "L2 行为回归读数" in text]
    assert readings, f"L2 读数在事件流里查不到：{notes}"
    assert "逐字不一致" in readings[0], readings[0]
