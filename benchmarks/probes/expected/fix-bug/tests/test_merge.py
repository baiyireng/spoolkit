"""merge_slots 的回归测试。

    python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slots.merge import merge_slots  # noqa: E402


class TestMergeSlots(unittest.TestCase):
    def test_空输入(self):
        self.assertEqual(merge_slots([]), [])

    def test_不重叠的保持原样(self):
        self.assertEqual(
            merge_slots([("09:00", "10:00"), ("14:00", "15:00")]),
            [("09:00", "10:00"), ("14:00", "15:00")],
        )

    def test_重叠的并起来(self):
        self.assertEqual(
            merge_slots([("09:00", "10:30"), ("10:00", "11:00")]),
            [("09:00", "11:00")],
        )

    def test_被包住的并起来(self):
        self.assertEqual(
            merge_slots([("09:00", "12:00"), ("10:00", "11:00")]),
            [("09:00", "12:00")],
        )

    def test_乱序输入也要有序输出(self):
        self.assertEqual(
            merge_slots([("14:00", "15:00"), ("09:00", "10:00")]),
            [("09:00", "10:00"), ("14:00", "15:00")],
        )

    def test_首尾相接的并起来(self):
        self.assertEqual(
            merge_slots([("09:00", "10:00"), ("10:00", "11:00")]),
            [("09:00", "11:00")],
        )

    def test_三段时间首尾相接(self):
        self.assertEqual(
            merge_slots([("09:00", "10:00"), ("10:00", "11:00"), ("11:00", "12:00")]),
            [("09:00", "12:00")],
        )


if __name__ == "__main__":
    unittest.main()
