import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gamesim import turn
from tests.helpers import dump, move, small_entities, write_entities

REPO = Path(__file__).resolve().parents[1]


def read(d, rel):
    return open(os.path.join(d, rel), encoding="utf-8").read()


class TestTurn(unittest.TestCase):
    def test_legacy_flow_has_dice_and_no_placeholders(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities())
            turn.cmd_init(d)
            turn.cmd_prep(d, 1)
            p = read(d, "turns/r01/prompt_Alpha.md")
            self.assertNotIn("{{", p)
            self.assertNotIn("秘密B", p)          # private info isolation
            self.assertNotIn("基准率上限", p)      # flags off
            self.assertIn("Write 工具", p)         # file mode
            for c in ("Alpha", "Bravo"):
                dump(move(3), os.path.join(d, f"turns/r01/move_{c}.json"))
            turn.cmd_gm(d, 1)
            g = read(d, "turns/r01/prompt_gm.md")
            self.assertIn("骰子 r < p_gm 就判成功", g)
            self.assertNotIn("{{", g)

    def test_blind_flow_commit_roll_apply(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities({"blind_dice": True, "base_rate_cap": True, "reference_class": True,
                                              "discount": True}))
            turn.cmd_init(d)
            turn.cmd_prep(d, 1)
            p = read(d, "turns/r01/prompt_Alpha.md")
            self.assertIn("基准率上限", p)
            self.assertIn("δ = 0.8", p)
            for c in ("Alpha", "Bravo"):
                dump(move(3), os.path.join(d, f"turns/r01/move_{c}.json"))
            turn.cmd_gm(d, 1)
            commit_prompt = read(d, "turns/r01/prompt_gm_commit.md")
            dice = json.load(open(os.path.join(d, "turns/r01/dice.json")))
            self.assertNotIn(str(dice["甲"]), commit_prompt)   # GM cannot see dice
            self.assertNotIn("{{", commit_prompt)
            dump({"probs": {"甲": {"p_gm": 1.0}, "乙": {"p_gm": 0.0}}, "exogenous": [{"event": "选举", "p": 1.0}]},
                 os.path.join(d, "turns/r01/gm_commit.json"))
            turn.cmd_roll(d, 1)
            oc = json.load(open(os.path.join(d, "turns/r01/outcomes.json")))
            self.assertTrue(oc["actions"]["甲"]["succeeded"])
            self.assertFalse(oc["actions"]["乙"]["succeeded"])
            self.assertTrue(oc["exogenous"][0]["happened"])
            resolve = read(d, "turns/r01/prompt_gm.md")
            self.assertIn("成败已定", resolve)
            self.assertIn("先写参照类", resolve)
            self.assertIn("p_target_ci90", resolve)
            # GM tries to flip outcomes; apply must enforce the rolled ones. Old-style `ceasefire` key still counts.
            dump({"state_vars": {"H": 120, "W": 20}, "public_state": "x", "last_round_public": "y",
                  "resolution": [{"entity": "甲", "succeeded": False}, {"entity": "乙", "succeeded": True}],
                  "ceasefire": {"in_effect": True}}, os.path.join(d, "turns/r01/gm.json"))
            turn.cmd_apply(d, 1)
            rnd = json.load(open(os.path.join(d, "rounds/round_01.json")))
            res = {x["entity"]: x["succeeded"] for x in rnd["gm"]["resolution"]}
            self.assertEqual(res, {"甲": True, "乙": False})
            self.assertEqual(rnd["gm"]["state_vars"]["H"], 100)   # clipped to hi
            st = json.load(open(os.path.join(d, "state.json")))
            self.assertTrue(st["history"][0]["hit"])
            self.assertTrue(st["history"][0]["recovered"])        # alias

    def test_stdout_mode_prompts(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities({"blind_dice": True}))
            turn.cmd_init(d)
            res = turn.cmd_prep(d, 1, io="stdout")
            self.assertEqual([r["code"] for r in res["roles"]], ["Alpha", "Bravo"])
            p = read(d, "turns/r01/prompt_Alpha.md")
            self.assertIn("只输出一个 JSON 代码块", p)
            self.assertNotIn("Write 工具", p)
            self.assertNotIn("move_Alpha.json", p)               # no file paths in stdout mode
            for c in ("Alpha", "Bravo"):
                dump(move(3), os.path.join(d, f"turns/r01/move_{c}.json"))
            turn.cmd_gm(d, 1, io="stdout")
            g = read(d, "turns/r01/prompt_gm_commit.md")
            self.assertIn("只输出一个 JSON 代码块", g)
            self.assertNotIn("Write 工具", g)

    def test_periods_target_and_agreement_rendered(self):
        with tempfile.TemporaryDirectory() as d:
            data = small_entities()
            write_entities(d, data)
            turn.cmd_init(d)
            turn.cmd_prep(d, 1)
            per = data["periods"][0]
            p = read(d, "turns/r01/prompt_Alpha.md")
            self.assertIn(f"本轮是 {per['start']}–{per['end']}", p)
            self.assertIn("停火底线", p)
            self.assertIn("agreement_reservation", p)
            for c in ("Alpha", "Bravo"):
                dump(move(3), os.path.join(d, f"turns/r01/move_{c}.json"))
            turn.cmd_gm(d, 1)
            g = read(d, "turns/r01/prompt_gm.md")
            self.assertIn(f"本轮是 {per['start']}–{per['end']}", g)
            self.assertIn("H ≥ 95（流量）", g)
            self.assertIn("W ≤ 30（烈度）", g)
            self.assertIn("`agreement.in_effect`", g)
            self.assertIn(f"到 {data['periods'][-1]['end']} 为止", g)
            self.assertIn('"p_target"', g)
            self.assertNotIn("p_recovery", g)

    def test_no_agreement_means_no_agreement_sections(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities(agreement=False))
            turn.cmd_init(d)
            turn.cmd_prep(d, 1)
            p = read(d, "turns/r01/prompt_Alpha.md")
            self.assertNotIn("底线", p)
            self.assertNotIn("agreement", p)
            for c in ("Alpha", "Bravo"):
                dump(move(3), os.path.join(d, f"turns/r01/move_{c}.json"))
            turn.cmd_gm(d, 1)
            g = read(d, "turns/r01/prompt_gm.md")
            self.assertNotIn('"agreement": {', g)
            self.assertNotIn("裁决**", g)

    def test_seed_from_id_drives_dice(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "run_1"), os.path.join(d, "run_2")
            for run, sid in ((a, "gs_20261103_ab12/run_1"), (b, "gs_20261103_ab12/run_2")):
                write_entities(run, small_entities())
                turn.cmd_init(run, seed_id=sid)
                turn.cmd_prep(run, 1)
            da = json.load(open(os.path.join(a, "turns/r01/dice.json")))
            db = json.load(open(os.path.join(b, "turns/r01/dice.json")))
            self.assertNotEqual(da, db)
            self.assertEqual(json.load(open(os.path.join(a, "run.json")))["seed"],
                             int.from_bytes(__import__("hashlib").sha1(b"gs_20261103_ab12/run_1").digest()[:4], "big"))

    def test_init_from_master_entities(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities())
            run = os.path.join(d, "run_1")
            turn.cmd_init(run, from_entities=os.path.join(d, "entities.json"), seed_id="x/run_1")
            self.assertTrue(os.path.exists(os.path.join(run, "entities.json")))
            self.assertTrue(os.path.exists(os.path.join(run, "private", "Alpha.json")))

    def test_bad_move_raises(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities())
            turn.cmd_init(d)
            turn.cmd_prep(d, 1)
            dump(move(3), os.path.join(d, "turns/r01/move_Alpha.json"))
            dump({"chosen": {}}, os.path.join(d, "turns/r01/move_Bravo.json"))
            with self.assertRaises(turn.MoveError) as cm:
                turn.cmd_gm(d, 1)
            self.assertIn("chosen.escalation", str(cm.exception))


class TestCli(unittest.TestCase):
    def run_cli(self, args, **kw):
        return subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True, **kw)

    def test_runs_as_script_and_as_module(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities())
            r = self.run_cli(["gamesim/turn.py", "init", d])
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_cli(["-m", "gamesim.turn", "prep", d, "1", "--io", "stdout", "--json"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["io"], "stdout")
            r = self.run_cli(["-m", "gamesim.turn", "gm", d, "1"])
            self.assertEqual(r.returncode, 2)                       # moves missing -> INVALID
            self.assertIn("INVALID", r.stdout)
            r = self.run_cli(["-m", "gamesim.turn", "apply", d, "1"])
            self.assertEqual(r.returncode, 2)                       # gm.json missing -> ERROR
            self.assertIn("ERROR", r.stderr)

    def test_status_script(self):
        with tempfile.TemporaryDirectory() as d:
            run = os.path.join(d, "run_1")
            write_entities(run, small_entities())
            self.assertEqual(self.run_cli(["gamesim/turn.py", "init", run]).returncode, 0)
            r = self.run_cli(["gamesim/status.py", d, "--json"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout), {"run_1": {"step": "prep", "round": 1}})


if __name__ == "__main__":
    unittest.main()
