"""管道各层的回归测试。

    python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.fetch import load_rows  # noqa: E402
from pipeline.report import render  # noqa: E402
from pipeline.transform import load_and_normalize  # noqa: E402

DATA = ROOT / "data" / "parts.csv"


class TestPipeline(unittest.TestCase):
    def test_抓取层读到四行(self):
        rows = load_rows(DATA)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["name"], "齿轮箱")

    def test_变换层丢掉空名字(self):
        rows = load_and_normalize(DATA)
        self.assertEqual(len(rows), 3)

    def test_报表合计(self):
        text = render(DATA)
        self.assertIn("齿轮箱", text)
        self.assertIn("合计 242.75", text)
        self.assertNotIn("9.90", text)

    def test_报表不包含空名字那一行(self):
        lines = render(DATA).splitlines()
        self.assertEqual(len(lines), 4)


if __name__ == "__main__":
    unittest.main()
