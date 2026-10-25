"""fix-bug 的验收判据。

两条腿：

1. **项目自己的测试全过**——修 bug 的定义就是这个。另外钉住测试文件里
   「首尾相接也要合并」那几条用例还在（至少 6 个 test 方法），删测试不算修好。
2. **行为层面直接断言**：判据自己 import 模块跑一遍，不看测试报告也不看模型
   的总结文字。测试文件被删、被 skip、被改成 `assertTrue(True)` 都骗不过这一条。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checker, child_env, parse_args, run  # noqa: E402

CASES = [
    ([], [], "空输入"),
    ([("09:00", "10:00"), ("14:00", "15:00")], [("09:00", "10:00"), ("14:00", "15:00")], "不重叠的原样"),
    ([("09:00", "10:30"), ("10:00", "11:00")], [("09:00", "11:00")], "重叠的合起来"),
    ([("09:00", "12:00"), ("10:00", "11:00")], [("09:00", "12:00")], "被包住的合起来"),
    ([("09:00", "10:00"), ("10:00", "11:00")], [("09:00", "11:00")], "首尾相接的合起来"),
    (
        [("09:00", "10:00"), ("10:00", "11:00"), ("11:00", "12:00")],
        [("09:00", "12:00")],
        "三连相接的合成一段",
    ),
    ([("14:00", "15:00"), ("09:00", "10:00")], [("09:00", "10:00"), ("14:00", "15:00")], "乱序输入有序输出"),
]

MIN_TEST_METHODS = 6


def main():
    args = parse_args(__doc__)
    root = Path(args.root).resolve()
    checker = Checker("fix-bug")

    test_file = root / "tests" / "test_merge.py"
    if checker.require(test_file.is_file(), "tests/test_merge.py 还在"):
        text = test_file.read_text(encoding="utf-8", errors="replace")
        found = len(re.findall(r"def (test_\w+)", text))
        checker.require(
            found >= MIN_TEST_METHODS,
            f"测试文件里仍有 {MIN_TEST_METHODS} 个以上用例（实际 {found}）",
        )

    sys.path.insert(0, str(root))
    for name in [key for key in list(sys.modules) if key == "slots" or key.startswith("slots.")]:
        del sys.modules[name]
    try:
        from slots.merge import merge_slots
    except Exception as exc:  # noqa: BLE001
        checker.require(False, f"slots.merge 能导入（实际：{exc!r}）")
        return checker.finish()

    for slots, want, label in CASES:
        try:
            got = merge_slots(list(slots))
        except Exception as exc:  # noqa: BLE001
            checker.require(False, f"{label}：抛了 {exc!r}")
            continue
        checker.require(
            list(got) == list(want),
            f"{label}：{slots} -> {want}（实际 {got}）",
        )

    code, out, err, timed_out = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=root,
        timeout=180,
        env=child_env(root),
    )
    checker.require(not timed_out, "项目测试没有挂死")
    checker.require(code == 0, f"项目自己的测试全过（退出码 {code}）")
    if code != 0:
        checker.note(f"  {' '.join((err or out).split())[-400:]}")

    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
