"""strategy (layer 2) with the fake scorer, plus the pure utility arithmetic."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from gamesim import runner, strategy
from gamesim.fsio import jload
from tests.fake_env import calls, fake_claude
from tests.helpers import dump, small_entities

VARS = {"H": {"sym": "H", "name": "流量", "lo": 0, "hi": 100, "current": 80},
        "W": {"sym": "W", "name": "烈度", "lo": 0, "hi": 100, "current": 50}}

CFG = {
    "role": "测试", "impact_marker": "影响栏",
    "F": {"cap": 100, "components": [{"key": "money", "name": "收入", "weight": 0.6, "rule": "…"},
                                     {"key": "family", "name": "团聚", "weight": 0.4, "rule": "…"}]},
    "arms": [{"id": "A1", "name": "留下"}, {"id": "A2", "name": "离开"}],
    "exposure": {"H": [1, 0.5]},
    "decision_makers": [
        {"id": "甲", "name": "决策者甲", "profile": {"身份": "<填写>"}, "discount_per_round": 0.9,
         "utility": [{"var": "F", "weight": 0.6, "direction": "+", "shape": "linear"},
                     {"var": "H", "weight": 0.4, "direction": "+", "shape": "linear"}],
         "red_lines": [{"rule": "H 归零时留下", "var": "H", "op": "<=", "threshold": 5, "arms": ["A1"]}]},
        {"id": "乙", "name": "决策者乙", "discount_per_round": 1.0,
         "utility": [{"var": "F", "weight": 1.0, "direction": "+", "shape": "concave"}],
         "var_mods": [{"arms": ["A1"], "var": "H", "add": -500}]}],
    "sensitivity": [{"name": "exp-30", "exposure_scale": 0.7}, {"name": "money-30", "component_scale": {"money": 0.7}}]}


def obj_for(cfg, n, a1=80, a2=60):
    return {"arms": {"A1": [{"money": a1, "family": a1} for _ in range(n)],
                     "A2": [{"money": a2, "family": a2} for _ in range(n)]}}


class TestArithmetic(unittest.TestCase):
    def setUp(self):
        self.cfg = strategy.load_config_dict(copy.deepcopy(CFG))
        self.dm = self.cfg["decision_makers"][0]

    def test_f_value_and_cap(self):
        self.assertAlmostEqual(strategy.f_value(self.cfg, {"money": 50, "family": 100}), 70.0)
        self.assertAlmostEqual(strategy.f_value(self.cfg, {"money": 50, "family": 100}, {"money": 2}), 100.0)

    def test_utility_exposure_and_red_line(self):
        u1 = strategy.dm_utility(self.cfg, self.dm, 0, 50, {"H": 100, "W": 0}, VARS)
        u2 = strategy.dm_utility(self.cfg, self.dm, 1, 50, {"H": 100, "W": 0}, VARS)
        self.assertAlmostEqual(u1, 0.6 * 0 + 0.4 * 1)          # F=50 -> u 0; H=100 -> u 1
        self.assertAlmostEqual(u2, 0.4 * 0.5)                   # exposure 0.5
        self.assertEqual(strategy.dm_utility(self.cfg, self.dm, 0, 50, {"H": 3, "W": 0}, VARS), -10.0)
        self.assertNotEqual(strategy.dm_utility(self.cfg, self.dm, 1, 50, {"H": 3, "W": 0}, VARS), -10.0)

    def test_exposure_scale_caps_at_one(self):
        self.assertEqual(strategy.exposure(self.cfg, self.dm, "H", 1, 1.3), 0.65)
        self.assertEqual(strategy.exposure(self.cfg, self.dm, "H", 0, 0.5), 1.0)   # exposure 1 is not scaled
        self.assertEqual(strategy.exposure(self.cfg, self.dm, "W", 1), 1.0)       # not in table
        dm = {**self.dm, "exposure_mult": {"H": 0.4}}
        self.assertAlmostEqual(strategy.exposure(self.cfg, dm, "H", 0), 0.4)

    def test_var_mods_clip_to_range(self):
        x = strategy.effective_state(self.cfg["decision_makers"][1], "A1", {"H": 80}, VARS)
        self.assertEqual(x["H"], 0)
        self.assertEqual(strategy.effective_state(self.cfg["decision_makers"][1], "A2", {"H": 80}, VARS)["H"], 80)

    def test_discounted_totals_and_rank(self):
        rows = [{"state": {"H": 100, "W": 0}}] * 2
        t = strategy.totals(self.cfg, self.dm, obj_for(self.cfg, 2, 100, 50), rows, VARS)
        self.assertAlmostEqual(t["A1"], round(1.0 + 0.9 * 1.0, 3))   # u = .6·1 + .4·1 each round
        rk = strategy.rank({"r1": {"A1": 1, "A2": 2}, "r2": {"A1": 3, "A2": 0}}, {"r1": 0.25, "r2": 0.75},
                           ["A1", "A2"])
        self.assertEqual([x["arm"] for x in rk["ranking"]], ["A1", "A2"])
        self.assertEqual(rk["top_per_run"], {"r1": "A2", "r2": "A1"})
        self.assertEqual(rk["consistent"], "1/2")

    def test_validate(self):
        errs, warns = strategy.validate(self.cfg, VARS)
        self.assertEqual(errs, [])
        bad = copy.deepcopy(CFG)
        bad["decision_makers"][0]["utility"][1].update(direction="-", shape="convex")
        bad["decision_makers"][0]["utility"].append({"var": "Q", "weight": 0.5, "shape": "step"})
        bad["exposure"]["H"] = [1]
        errs, warns = strategy.validate(strategy.load_config_dict(bad), VARS)
        self.assertTrue(any("权重之和" in e for e in errs))
        self.assertTrue(any("Q" in e for e in errs))
        self.assertTrue(any("step" in e for e in errs))
        self.assertTrue(any("长度" in e for e in errs))
        self.assertTrue(any("concave" in w for w in warns))
        self.assertFalse(any("discount_per_round" in w for w in warns))

    def test_check_scores(self):
        self.assertEqual(strategy.check_scores(obj_for(self.cfg, 2), self.cfg, 2), [])
        self.assertTrue(strategy.check_scores(obj_for(self.cfg, 1), self.cfg, 2))
        o = obj_for(self.cfg, 2)
        o["arms"]["A2"][1]["money"] = 120
        self.assertIn("A2 第 2 轮 money 必须是 0–100 的数字", strategy.check_scores(o, self.cfg, 2))


class TestScoreWithFakeScorer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.base = os.path.join(cls.tmp, "base")
        src = os.path.join(cls.tmp, "entities.json")
        dump(small_entities(), src)
        with fake_claude():
            runner.run_all(cls.base, src, 2, tries=0)
        cls.cfg_path = os.path.join(cls.tmp, "strategy.json")
        dump(CFG, cls.cfg_path)
        cls.log = os.path.join(cls.tmp, "calls.log")
        with fake_claude(log_path=cls.log):
            cls.s = strategy.score_all(cls.cfg_path, cls.base)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_one_scorer_call_per_run_and_dm_cached(self):
        self.assertEqual(calls(self.log), {"scorer": 4})
        with fake_claude(log_path=self.log):
            strategy.score_all(self.cfg_path, self.base)
        self.assertEqual(calls(self.log), {"scorer": 4})
        o = jload(os.path.join(self.base, "layer2", "run_1_甲.json"))
        self.assertEqual(len(o["arms"]["A1"]), 2)

    def test_summary(self):
        d = self.s["by_dm"]["甲"]["scenarios"]
        self.assertEqual(set(d), {"base", "exp-30", "money-30"})
        self.assertEqual(d["base"]["ranking"][0]["arm"], "A1")    # fake scores arm 1 higher, exposure 1 > .5
        self.assertEqual(d["base"]["consistent"], "2/2")
        self.assertIn("flip", d["exp-30"])
        dec = self.s["by_dm"]["甲"]["decomposition"]["A1"]
        self.assertTrue({"F", "money", "family", "nonF_U"} <= set(dec))
        self.assertEqual(self.s["weights"], {"run_1": 0.5, "run_2": 0.5})
        self.assertTrue(os.path.exists(os.path.join(self.base, "layer2", "summary.md")))
        self.assertIn("### 甲", self.s["markdown"])

    def test_prompt_carries_rules_and_trajectory(self):
        rows, side = strategy.trajectory(os.path.join(self.base, "run_1"), strategy.load_config(self.cfg_path))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["impact"], "（GM 未单列）")
        self.assertIsNone(side)
        cfg = strategy.load_config(self.cfg_path)
        p = strategy.build_prompt(cfg, cfg["decision_makers"][0], rows, side)
        for s in ("打分员", "决策者：`甲`", "`A1`", "`money`", "身份：<填写>", "不要调用任何工具"):
            self.assertIn(s, p)

    def test_sweep_finds_red_line_flip(self):
        res = strategy.sweep(self.cfg_path, self.base, "run_1", 1, {"H": [80, 0]}, ["甲"])
        self.assertFalse(res["甲 H=80"]["flip"])
        self.assertTrue(res["甲 H=0"]["flip"])                    # red line on A1 -> A2 wins
        self.assertEqual(res["甲 H=0"]["ranking"][0], "A2")
        self.assertTrue(os.path.exists(os.path.join(self.base, "layer2", "sweep_run_1.json")))

    def test_scorer_retry_keeps_raw_and_appends_error(self):
        cfg = strategy.load_config(self.cfg_path)
        out = os.path.join(self.tmp, "out_retry")
        os.makedirs(out)
        prompts = []

        def call(prompt, model, timeout):
            prompts.append(prompt)
            if len(prompts) == 1:
                return "我先想想"
            return "```json\n" + json.dumps(obj_for(cfg, 2), ensure_ascii=False) + "\n```"
        obj = strategy.score_one(os.path.join(self.base, "run_2"), cfg["decision_makers"][1], cfg, out, call)
        self.assertEqual(len(obj["arms"]["A2"]), 2)
        self.assertIn("上次输出不合格", prompts[1])
        self.assertTrue(os.path.exists(os.path.join(out, "raw_run_2_乙_0.txt")))

    def test_bad_config_is_rejected_before_any_call(self):
        bad = copy.deepcopy(CFG)
        bad["decision_makers"][0]["utility"][0]["weight"] = 0.1
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(bad, f, ensure_ascii=False)
        with self.assertRaises(ValueError):
            strategy.score_all(p, self.base, out_dir=os.path.join(self.tmp, "out_bad"))


if __name__ == "__main__":
    unittest.main()
