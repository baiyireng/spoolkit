"""add-feature 的验收判据。

「加一个按月汇总」要同时满足三件事，缺一条都不算做完：

1. `python -m expenses.cli summary --month 2025-03` 真能跑（退出码 0），
   并且把**这个月**的总额与分类小计打出来——数字逐字对得上账本；
2. 别的月份**没有被算进来**（2025-02 的 88.00、2025-04 的 45.00 不许出现），
   否则「按月」这件事是假的；
3. 既有的 `total` / `by-category` 与项目自己的测试都没被改坏。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checker, child_env, parse_args, run  # noqa: E402

MONTH = "2025-03"
# 2025-03 的账：餐饮 348.00、交通 64.50、办公 99.90，合计 512.40
MONTH_TOTAL = "512.40"
MONTH_LINES = [("餐饮", "348.00"), ("交通", "64.50"), ("办公", "99.90")]
# 其他两月的金额，按月汇总里出现任何一个都说明月份过滤没生效
OTHER_MONTH_AMOUNTS = ["88.00", "32.00", "45.00", "12.00"]
WHOLE_BOOK_TOTAL = "689.40"


def cli(root, argv, timeout=120):
    return run(
        [sys.executable, "-m", "expenses.cli", *argv],
        cwd=root,
        timeout=timeout,
        env=child_env(root),
    )


def main():
    args = parse_args(__doc__)
    root = Path(args.root).resolve()
    checker = Checker("add-feature")

    code, out, err, timed_out = cli(root, ["summary", "--month", MONTH])
    checker.require(not timed_out, "summary 没有挂死")
    checker.require(code == 0, f"summary --month {MONTH} 退出码 0（实际 {code}）")
    if code != 0:
        checker.note(f"  stderr: {' '.join(err.split())[:400]}")
        checker.note(f"  stdout: {' '.join(out.split())[:200]}")

    combined = out + "\n" + err
    checker.require(MONTH_TOTAL in combined, f"这个月总额 {MONTH_TOTAL} 出现在输出里")
    for name, amount in MONTH_LINES:
        checker.require(name in combined, f"分类 {name} 出现")
        checker.require(amount in combined, f"{name} 的金额 {amount} 出现")
    for amount in OTHER_MONTH_AMOUNTS:
        checker.require(
            amount not in combined,
            f"别的月份的金额 {amount} 没有混进来",
        )

    # 没用 --month 时的行为留给项目自己定，不判；既有两条命令必须原样好使。
    code, out, err, _ = cli(root, ["total"])
    checker.require(code == 0, "total 仍然退出码 0")
    checker.require(WHOLE_BOOK_TOTAL in (out + err), f"total 仍然是 {WHOLE_BOOK_TOTAL}")

    code, out, err, _ = cli(root, ["by-category"])
    checker.require(code == 0, "by-category 仍然退出码 0")
    for amount in ("481.00", "111.90", "96.50"):
        checker.require(amount in (out + err), f"by-category 里仍有 {amount}")

    # 换一个月也要对：2025-04 只有 45.00 + 12.00 = 57.00
    code, out, err, _ = cli(root, ["summary", "--month", "2025-04"])
    checker.require(code == 0, "summary --month 2025-04 退出码 0")
    checker.require("57.00" in (out + err), "2025-04 合计 57.00 出现")

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
