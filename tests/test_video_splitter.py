import unittest
from app.split_page import ranges

class SplitRangesTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(ranges(125, 0, 60), [(0, 60), (60, 120), (120, 125)])

    def test_count(self):
        self.assertEqual(ranges(90, 1, 3), [(0, 30), (30, 60), (60, 90)])

    def test_invalid(self):
        for value in (0, -1, float('nan')):
            with self.assertRaises(ValueError):
                ranges(90, 0, value)
