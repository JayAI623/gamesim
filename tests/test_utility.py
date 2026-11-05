import json
import os
import sys
import tempfile
import unittest

from gamesim import utility as ut  # noqa: E402

VARS = {"S": {"sym": "S", "lo": 0, "hi": 100, "current": 30},
        "O": {"sym": "O", "lo": 50, "hi": 150, "current": 115}}
US = {"name": "US", "utility": [{"var": "S", "weight": 0.6, "direction": "+", "shape": "linear"},
                                {"var": "O", "weight": 0.4, "direction": "-", "shape": "linear"}],
      "red_lines": [{"rule": "oil spike", "var": "O", "op": ">=", "threshold": 140}]}


class TestUtility(unittest.TestCase):
    def test_linear_bounds(self):
        self.assertAlmostEqual(ut.utility(US, VARS, {"S": 100, "O": 50})["U"], 1.0)
        self.assertAlmostEqual(ut.utility(US, VARS, {"S": 0, "O": 139})["U"], -0.6 + 0.4 * (2 * (1 - 0.89) - 1), places=3)

    def test_red_line(self):
        r = ut.utility(US, VARS, {"S": 100, "O": 145})
        self.assertEqual(r["U"], -10)
        self.assertEqual(r["red_lines_breached"], ["oil spike"])

    def test_shapes(self):
        v = VARS["S"]
        self.assertAlmostEqual(ut.term_value({"var": "S", "shape": "concave"}, v, 25), 2 * 0.5 - 1)
        self.assertAlmostEqual(ut.term_value({"var": "S", "shape": "convex"}, v, 50), 2 * 0.25 - 1)
        self.assertEqual(ut.term_value({"var": "S", "shape": "threshold", "threshold": 60}, v, 70), 1)
        self.assertEqual(ut.term_value({"var": "S", "shape": "threshold", "threshold": 60, "direction": "-"}, v, 70), -1)
        with self.assertRaises(ValueError):
            ut.term_value({"var": "S", "shape": "threshold"}, v, 70)

    def test_peak(self):
        v = VARS["S"]
        self.assertEqual(ut.term_value({"var": "S", "shape": "peak", "threshold": 70}, v, 70), 1)
        self.assertAlmostEqual(ut.term_value({"var": "S", "shape": "peak", "threshold": 70}, v, 0), -1)
        self.assertAlmostEqual(ut.term_value({"var": "S", "shape": "peak", "threshold": 70}, v, 85), 1 - 2 * 15 / 70)

    def test_clipping(self):
        self.assertEqual(ut.term_value({"var": "S"}, VARS["S"], 150), 1)

    def test_weight_check(self):
        with self.assertRaises(ValueError):
            ut.check_weights({"name": "x", "utility": [{"var": "S", "weight": 0.5}]})

    def test_table_from_run_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "rounds"))
            with open(os.path.join(d, "entities.json"), "w") as f:
                json.dump({"variables": list(VARS.values()), "entities": [US]}, f)
            with open(os.path.join(d, "rounds", "round_01.json"), "w") as f:
                json.dump({"round": 1, "moves": {}, "gm": {"state_vars": {"S": 60}}}, f)
            md = ut.table(d)
            self.assertIn("| 0 | 30 | 115 |", md)
            self.assertIn("| 1 | 60 | 115 |", md)


if __name__ == "__main__":
    unittest.main()
