"""记账命令行。

    python -m expenses.cli total
    python -m expenses.cli by-category
    python -m expenses.cli summary --month 2025-03
    python -m expenses.cli total 别的账本.csv

不给账本路径时读 `data/expenses.csv`。
"""

import argparse
import sys
from pathlib import Path

from expenses.store import by_category, in_month, load, total

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "expenses.csv"

USAGE = """用法：
  python -m expenses.cli total [账本文件]
  python -m expenses.cli by-category [账本文件]
  python -m expenses.cli summary --month YYYY-MM [账本文件]
"""

COMMANDS = ("total", "by-category", "summary")


def render_by_category(summary):
    """分类小计的多行文本：每行 `分类名 金额`，金额降序、同额按名字升序。"""
    ordered = sorted(summary.items(), key=lambda item: (-item[1], item[0]))
    return "\n".join(f"{name} {amount:.2f}" for name, amount in ordered)


def summary_options(argv):
    """把 summary 那一段参数解出来，返回（月份, 账本路径）。"""
    parser = argparse.ArgumentParser(prog="expenses.cli summary", add_help=False)
    parser.add_argument("--month", required=True)
    parser.add_argument("ledger", nargs="?")
    known, _unknown = parser.parse_known_args(argv)
    path = Path(known.ledger) if known.ledger else DEFAULT_DATA
    return known.month, path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2

    command = argv[0]
    if command not in COMMANDS:
        # 先认命令再看文件：不然 `summary --month 2025-03` 会拿 "--month"
        # 当账本路径去打开，报一个与真实原因无关的 FileNotFoundError。
        print(f"不认识这个子命令：{command}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    if command == "summary":
        month, path = summary_options(argv[1:])
        rows = in_month(load(path), month)
        print(f"{month} 合计：{total(rows):.2f}")
        if rows:
            print(render_by_category(by_category(rows)))
        return 0

    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_DATA
    rows = load(path)

    if command == "total":
        print(f"总支出：{total(rows):.2f}")
    else:
        print(render_by_category(by_category(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
