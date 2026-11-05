import json
import os
import sys
import tempfile
import unittest

from gamesim import compare  # noqa: E402


def move(pos, esc, opts, idx):
    return {"options_considered": opts, "chosen_index": idx,
            "chosen": {"action": opts[idx]["action"], "escalation": esc}, "position_now": pos}


GOOD = [{"action": "talk", "p_success": 0.5, "payoff_success": 4, "payoff_fail": 0, "eu": 2.0},
        {"action": "threaten", "p_success": 0.2, "payoff_success": 5, "payoff_fail": -5, "eu": -3.0}]
BAD_ARITH = [{"action": "talk", "p_success": 0.5, "payoff_success": 4, "payoff_fail": 0, "eu": 3.0}]


class TestCompare(unittest.TestCase):
    def test_audit_argmax_and_arith(self):
        self.assertTrue(compare.audit_move(move(50, 2, GOOD, 0))["picked_argmax"])
        self.assertFalse(compare.audit_move(move(50, 6, GOOD, 1))["picked_argmax"])
        self.assertEqual(compare.audit_move(move(50, 2, BAD_ARITH, 0))["arith_errors"], 1)

    def test_audit_tolerates_missing_fields(self):
        a = compare.audit_move({"chosen": {"escalation": 3}})
        self.assertIsNone(a["picked_argmax"])

    def test_summarise_run_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "rounds"))
            for r, (pa, pb) in enumerate([(20, 80), (30, 70)], start=1):
                with open(os.path.join(d, "rounds", f"round_{r:02d}.json"), "w") as f:
                    json.dump({"round": r, "moves": {"A": move(pa, 3, GOOD, 0), "B": move(pb, 7, GOOD, 1)},
                               "gm": {}}, f)
            with open(os.path.join(d, "bdm_result.json"), "w") as f:
                json.dump({"final_median": 40, "final_mean": 45, "history": [
                    {"round": 0, "positions": {"A": 20, "B": 80}},
                    {"round": 1, "positions": {"A": 25, "B": 60}}]}, f)
            md = compare.summarise(d)
            self.assertIn("| 1 | 7 | 5.0 | 3 | 7 |", md)
            self.assertIn("20 / 25", md)
            self.assertIn("| B | 2 | 0/2 |", md)
            self.assertIn("BDM 中位 40.0", md)

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("没有找到", compare.summarise(d))


if __name__ == "__main__":
    unittest.main()
