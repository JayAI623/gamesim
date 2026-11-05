"""The gamesim CLI (check / run --fake / status / score / sweep / bdm) and the bundled examples."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from gamesim import cli, strategy
from gamesim.conditions import apply_patch
from gamesim.fsio import jload
from tests.fake_env import fake_claude
from tests.helpers import dump, small_entities

REPO = Path(__file__).resolve().parents[1]
EX = REPO / "examples"


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.main(list(argv))
    return rc, out.getvalue()


class TestExamples(unittest.TestCase):
    def test_examples_pass_check(self):
        for name in ("hormuz", "diaspora"):
            rc, out = run_cli("check", str(EX / name / "entities.json"))
            self.assertEqual(rc, 0, out)

    def test_diaspora_swap_patches_cleanly(self):
        ent, sw = jload(EX / "diaspora" / "entities.json"), jload(EX / "diaspora" / "swap.json")
        for side, br in sw["branches"].items():
            d = apply_patch(ent, br["patch"])
            self.assertAlmostEqual(sum(t["weight"] for t in d["entities"][0]["utility"]), 1.0, places=3)
            self.assertNotIn("R1–R2 特朗普", " ".join(d["entities"][0]["soft_rules"]))
            self.assertTrue(br["event"])
        self.assertAlmostEqual(sum(sw["p"].values()), 1.0)

    def test_diaspora_strategy_validates(self):
        cfg = strategy.load_config(EX / "diaspora" / "strategy.json")
        variables = {v["sym"]: v for v in jload(EX / "diaspora" / "entities.json")["variables"]}
        errs, warns = strategy.validate(cfg, variables)
        self.assertEqual((errs, warns), ([], []))
        self.assertIn("<填写", json.dumps(cfg["decision_makers"], ensure_ascii=False))


    def test_pipeline_utility_prompt_matches_engine_template(self):
        a = (REPO / "prompts" / "pipeline" / "utility_research.md").read_text(encoding="utf-8")
        b = (REPO / "gamesim" / "prompts" / "utility_research.md").read_text(encoding="utf-8")
        self.assertEqual(a, b)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_check_flags_round0_red_line(self):
        d = small_entities()
        d["entities"][0]["red_lines"] = [{"rule": "H 高于 50 就行动", "var": "H", "op": ">=", "threshold": 50}]
        d["entities"][1]["utility"][0].update(direction="-", shape="convex")
        p = os.path.join(self.tmp, "e.json")
        dump(d, p)
        rc, out = run_cli("check", p)
        self.assertEqual(rc, 2)
        self.assertIn("第 0 轮就突破红线", out)
        self.assertIn("坑②", out)

    def test_check_flags_yearly_discount_without_delta(self):
        d = small_entities({"discount": True, "time_per_round": "1年"}, periods=False)
        p = os.path.join(self.tmp, "e.json")
        dump(d, p)
        rc, out = run_cli("check", p)
        self.assertEqual(rc, 0)
        self.assertIn("坑③", out)

    def test_run_status_score_sweep_bdm(self):
        src = os.path.join(self.tmp, "e.json")
        dump(small_entities({"blind_dice": True}), src)
        base = os.path.join(self.tmp, "base")
        rc, out = run_cli("run", base, "--entities", src, "--runs", "2", "--fake", "--tries", "0")
        self.assertEqual(rc, 0)
        self.assertIn("/2 局命中", out)
        rc, out = run_cli("status", base)
        self.assertIn("run_1: r2 done | fallback 0/4", out)
        cfg = os.path.join(self.tmp, "s.json")
        dump({"F": {"components": [{"key": "x", "weight": 1.0}]}, "arms": [{"id": "A"}, {"id": "B"}],
              "decision_makers": [{"id": "d", "discount_per_round": 1,
                                   "utility": [{"var": "F", "weight": 0.5}, {"var": "H", "weight": 0.5}]}]}, cfg)
        with fake_claude():
            rc, out = run_cli("score", cfg, base)
        self.assertEqual(rc, 0)
        self.assertIn("### d", out)
        rc, out = run_cli("sweep", cfg, base, "--run", "run_1", "--from-round", "1", "--set", "H=0,100")
        self.assertEqual(rc, 0)
        self.assertIn("d H=0", out)
        rc, out = run_cli("bdm", src, "-o", os.path.join(self.tmp, "bdm.json"), "--markdown")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "bdm.json")))

    def test_errors_exit_2(self):
        rc, _ = run_cli("run", os.path.join(self.tmp, "nothing"))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
