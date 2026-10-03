"""store 的回归测试：读、总额、分类小计。

    python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from expenses.store import by_category, load, total  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data" / "expenses.csv"


class TestStore(unittest.TestCase):
    def test_读整本账(self):
        rows = load(DATA)
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0]["date"], "2025-02-11")
        self.assertEqual(rows[0]["amount"], 88.0)

    def test_总额(self):
        self.assertAlmostEqual(total(load(DATA)), 689.40, places=2)

    def test_分类小计(self):
        summary = by_category(load(DATA))
        self.assertAlmostEqual(summary["餐饮"], 481.00, places=2)
        self.assertAlmostEqual(summary["交通"], 96.50, places=2)
        self.assertAlmostEqual(summary["办公"], 111.90, places=2)


if __name__ == "__main__":
    unittest.main()
