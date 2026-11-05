"""Replay the real hormuz v3 run_1 (trimmed fixture: model outputs only) through the generalized engine.

The engine must reproduce the original dice, blind outcomes, final state, private logs and terminal round.
"""
import json
import os
import tempfile
import unittest

from gamesim import aggregate, status, turn
from tests.helpers import FIXTURE, copy_fixture_files, fixture_entities, write_entities


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class TestHormuzReplay(unittest.TestCase):
    def replay(self, run, data, io):
        write_entities(run, data)
        turn.cmd_init(run)                                                # seed 201 from settings (legacy)
        for r in range(1, 7):
            td = os.path.join(run, "turns", f"r{r:02d}")
            fx = FIXTURE / "turns" / f"r{r:02d}"
            turn.cmd_prep(run, r, io)
            self.assertEqual(load(os.path.join(td, "dice.json")), load(fx / "dice.json"), r)
            copy_fixture_files(run, r, "move_*.json")
            turn.cmd_gm(run, r, io)
            copy_fixture_files(run, r, "gm_commit.json")
            oc = turn.cmd_roll(run, r, io)["outcomes"]
            self.assertEqual(oc, load(fx / "outcomes.json"), r)
            copy_fixture_files(run, r, "gm.json")
            turn.cmd_apply(run, r, attempt=1)

    def test_v08_replay_matches_original(self):
        with tempfile.TemporaryDirectory() as d:
            run = os.path.join(d, "run_1")
            self.replay(run, fixture_entities(), "stdout")
            st, fx = load(os.path.join(run, "state.json")), load(FIXTURE / "state.json")
            self.assertEqual(st["state_vars"], fx["state_vars"])
            self.assertEqual([h["state_vars"] for h in st["history"]], [h["state_vars"] for h in fx["history"]])
            self.assertEqual([h["p_target"] for h in st["history"]], [h["p_recovery_6m"] for h in fx["history"]])
            self.assertEqual([h["hit"] for h in st["history"]], [h["recovered"] for h in fx["history"]])
            self.assertEqual([h["agreement"]["in_effect"] for h in st["history"]],
                             [h["ceasefire"]["in_effect"] for h in fx["history"]])
            for code in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"):
                self.assertEqual(load(os.path.join(run, "private", f"{code}.json")),
                                 load(FIXTURE / "private" / f"{code}.json"), code)
            self.assertEqual(status.next_step(run), {"step": "done", "round": 6, "why": "terminal"})
            res = aggregate.compute_k([run], planned_n=1)
            self.assertEqual((res["k"], res["n"], res["complete"], res["kind"]), (0, 1, True, "ever"))
            self.assertEqual(res["runs"][0]["agreement_rounds"], 2)
            self.assertEqual(res["runs"][0]["p_target_last"], 0.32)
            md = aggregate.markdown(res)
            self.assertIn("| run_1 | 6/6 | 6 |", md)
            dg = aggregate.digest([run])
            self.assertIn("R6（2027-04-01–2027-04-30）", dg)
            # prompts carry the period line and the parameterized target
            g = open(os.path.join(run, "turns/r03/prompt_gm.md"), encoding="utf-8").read()
            self.assertIn("本轮是 ", g)
            self.assertIn("到 2027-04-30 为止", g)
            self.assertIn("`agreement.in_effect`", g)

    def test_legacy_entities_file_mode_still_runs(self):
        with tempfile.TemporaryDirectory() as d:
            run = os.path.join(d, "run_1")
            self.replay(run, fixture_entities(v08=False), "file")
            st = load(os.path.join(run, "state.json"))
            self.assertEqual(st["state_vars"], load(FIXTURE / "state.json")["state_vars"])
            p = open(os.path.join(run, "turns/r01/prompt_Alpha.md"), encoding="utf-8").read()
            self.assertIn("Write 工具", p)
            self.assertIn("停火条件（本次推演的起点）", p)          # ceasefire_context mapped to agreement 停火

    def test_fixture_is_not_modified(self):
        before = sorted(os.listdir(FIXTURE / "turns" / "r01"))
        with tempfile.TemporaryDirectory() as d:
            self.replay(os.path.join(d, "run_1"), fixture_entities(), "stdout")
        self.assertEqual(sorted(os.listdir(FIXTURE / "turns" / "r01")), before)
        self.assertFalse(os.path.exists(FIXTURE / "rounds"))


if __name__ == "__main__":
    unittest.main()
