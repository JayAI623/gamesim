import hashlib
import json
import os
import tempfile
import unittest

from gamesim import aggregate, status, turn
from gamesim import target as tg
from tests.helpers import HORMUZ_TARGET, dump, gm_out, play_round, small_entities, write_entities

T = HORMUZ_TARGET


class TestEvalTarget(unittest.TestCase):
    def test_all_conds_and_flags(self):
        self.assertTrue(tg.eval_target(T, {"H": 95, "W": 30}, {"agreement": {"in_effect": True}}))

    def test_cond_fails(self):
        self.assertFalse(tg.eval_target(T, {"H": 94.9, "W": 30}, {"agreement": {"in_effect": True}}))
        self.assertFalse(tg.eval_target(T, {"H": 99, "W": 31}, {"agreement": {"in_effect": True}}))

    def test_gm_flag_required(self):
        self.assertFalse(tg.eval_target(T, {"H": 99, "W": 10}, {"agreement": {"in_effect": False}}))
        self.assertFalse(tg.eval_target(T, {"H": 99, "W": 10}, {}))
        self.assertTrue(tg.eval_target(T, {"H": 99, "W": 10}, {"agreement": {"in_effect": "true"}}))

    def test_nested_flag_paths(self):
        t = {"conds": [], "gm_flags": ["deal.signed", "board.approved"]}
        self.assertTrue(tg.eval_target(t, {}, {"deal": {"signed": True}, "board": {"approved": True}}))
        self.assertFalse(tg.eval_target(t, {}, {"deal": {"signed": True}}))

    def test_missing_var_or_target(self):
        self.assertFalse(tg.eval_target(T, {"H": 99}, {"agreement": {"in_effect": True}}))
        self.assertFalse(tg.eval_target(None, {"H": 99, "W": 0}, {}))
        self.assertFalse(tg.eval_target({"conds": [], "gm_flags": []}, {}, {}))

    def test_ops(self):
        for op, v, want in ((">", 95, False), ("<", 96, True), ("==", 95, True), ("<=", 94, False)):
            self.assertEqual(tg.eval_target({"conds": [{"var": "H", "op": op, "value": v}]}, {"H": 95}, {}), want, op)


class TestKinds(unittest.TestCase):
    def test_ever(self):
        self.assertTrue(tg.run_hit([False, True, False], "ever"))
        self.assertFalse(tg.run_hit([False] * 6, "ever"))

    def test_at_deadline_uses_last_round(self):
        self.assertFalse(tg.run_hit([True, True, False], "at_deadline"))
        self.assertTrue(tg.run_hit([False, False, True], "at_deadline"))
        self.assertFalse(tg.run_hit([], "at_deadline"))

    def test_sustained(self):
        self.assertTrue(tg.run_hit([True, True, False, True], "sustained", 2))
        self.assertFalse(tg.run_hit([True, False, True, False, True], "sustained", 2))
        with self.assertRaises(ValueError):
            tg.run_hit([True], "sustained")

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            tg.run_hit([True], "sometimes")


class TestPeriodsAndSeed(unittest.TestCase):
    def test_compute_periods_covers_span(self):
        ps = tg.compute_periods("2026-11-03", "2027-04-30", 6)
        self.assertEqual(len(ps), 6)
        self.assertEqual(ps[0]["start"], "2026-11-03")
        self.assertEqual(ps[-1]["end"], "2027-04-30")
        for a, b in zip(ps, ps[1:]):
            from datetime import date, timedelta
            self.assertEqual(date.fromisoformat(a["end"]) + timedelta(days=1), date.fromisoformat(b["start"]))
        with self.assertRaises(ValueError):
            tg.compute_periods("2026-11-03", "2026-11-05", 6)

    def test_months_per_round(self):
        self.assertEqual(tg.months_per_round("2026-11-03", "2027-04-30"), 1)
        self.assertEqual(tg.months_per_round("2026-11-03", "2027-10-31"), 2)
        self.assertEqual(tg.months_per_round("2026-11-03", "2027-02-01"), 1)

    def test_seed_is_first_4_bytes_of_sha1(self):
        want = int.from_bytes(hashlib.sha1("gs_20261103_0001".encode()).digest()[:4], "big")
        self.assertEqual(tg.seed_from_id("gs_20261103_0001"), want)
        self.assertEqual(tg.seed_from_id("x"), tg.seed_from_id("x"))


class TestRunsAndAggregate(unittest.TestCase):
    def make(self, d, name, gms, rounds=3, **kw):
        run = os.path.join(d, name)
        write_entities(run, small_entities(rounds=rounds, **kw))
        turn.cmd_init(run, seed_id=f"gs/{name}")
        for r, gm in enumerate(gms, start=1):
            play_round(run, r, gm)
        return run

    def test_hit_written_with_alias(self):
        with tempfile.TemporaryDirectory() as d:
            run = self.make(d, "run_1", [gm_out(96, 20, True), gm_out(80, 50, False)])
            hist = json.load(open(os.path.join(run, "state.json")))["history"]
            self.assertEqual([h["hit"] for h in hist], [True, False])
            self.assertEqual([h["recovered"] for h in hist], [True, False])
            self.assertEqual(hist[0]["p_target"], 0.3)

    def test_terminal_is_absorbing(self):
        with tempfile.TemporaryDirectory() as d:
            run = self.make(d, "run_1", [gm_out(96, 20, True, terminal=True)])
            with self.assertRaises(turn.StepError):
                turn.cmd_prep(run, 2)
            self.assertEqual(status.next_step(run), {"step": "done", "round": 1, "why": "terminal"})
            res = aggregate.compute_k([run], kind="at_deadline")
            self.assertTrue(res["complete"])
            self.assertEqual(res["runs"][0]["rounds_played"], 1)
            self.assertEqual((res["k"], res["n"]), (1, 1))         # last state = absorbing state, still a hit

    def test_k_for_each_kind(self):
        with tempfile.TemporaryDirectory() as d:
            hit, miss = gm_out(96, 20, True), gm_out(80, 50, False)
            runs = [self.make(d, "run_1", [hit, miss, miss]),
                    self.make(d, "run_2", [miss, hit, hit]),
                    self.make(d, "run_3", [miss, miss, miss])]
            self.assertEqual(aggregate.compute_k(runs, "ever")["k"], 2)
            self.assertEqual(aggregate.compute_k(runs, "at_deadline")["k"], 1)
            self.assertEqual(aggregate.compute_k(runs, "sustained", 2)["k"], 1)
            res = aggregate.compute_k(runs)                         # kind from entities.target
            self.assertEqual((res["kind"], res["k"], res["n"], res["complete"]), ("ever", 2, 3, True))
            lo, hi = res["wilson95"]
            self.assertTrue(0 < lo < 2 / 3 < hi < 1)

    def test_n_is_planned_and_incomplete_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            hit, miss = gm_out(96, 20, True), gm_out(80, 50, False)
            done = self.make(d, "run_1", [hit, miss, miss])
            half = self.make(d, "run_2", [hit])
            res = aggregate.compute_k([done, half], planned_n=3)
            self.assertEqual(res["n"], 3)
            self.assertFalse(res["complete"])
            self.assertIn("run_2", res["incomplete"])
            self.assertIn("1 planned run(s) missing", res["incomplete"])
            self.assertIn("不登记预测", aggregate.markdown(res))

    def test_missing_var_renders_dash(self):
        with tempfile.TemporaryDirectory() as d:
            run = self.make(d, "run_1", [gm_out(96, 20, True)], rounds=1)
            res = aggregate.compute_k([run])
            res["runs"][0]["target_vars"]["W"] = None
            res["runs"][0]["p_target_last"] = None
            md = aggregate.markdown(res)
            self.assertIn("96/-", md)
            self.assertIn("| - (-) |", md)

    def test_legacy_run_dir_reads_recovered(self):
        """Pre-v0.8 run dir: no run.json, no commit.json, no target, history uses `recovered` + p_recovery_6m."""
        with tempfile.TemporaryDirectory() as d:
            run = os.path.join(d, "run_1")
            write_entities(run, small_entities(target=False, periods=False, agreement=False, rounds=2))
            os.makedirs(os.path.join(run, "rounds"))
            for r in (1, 2):
                dump({"round": r, "moves": {"甲": {"chosen": {"escalation": 3}}, "乙": {"chosen": {"escalation": 5}}},
                      "gm": {"state_vars": {"H": 90 + r, "W": 20}, "terminal": False}},
                     os.path.join(run, "rounds", f"round_{r:02d}.json"))
            dump({"round": 2, "public_state": "", "last_round_public": "", "state_vars": {"H": 92, "W": 20},
                  "history": [{"round": 1, "state_vars": {"H": 91, "W": 20}, "recovered": True,
                               "p_recovery_6m": 0.4, "ceasefire": {"in_effect": True}},
                              {"round": 2, "state_vars": {"H": 92, "W": 20}, "recovered": False,
                               "p_recovery_6m": 0.5, "p_recovery_ci90": [0.2, 0.7]}]},
                 os.path.join(run, "state.json"))
            res = aggregate.compute_k([run], kind="ever")
            r0 = res["runs"][0]
            self.assertEqual(r0["hits"], [True, False])
            self.assertEqual((res["k"], res["complete"]), (1, True))
            self.assertEqual(r0["p_target_last"], 0.5)
            self.assertEqual(r0["agreement_rounds"], 1)
            self.assertEqual(status.next_step(run), {"step": "done", "round": 2})


if __name__ == "__main__":
    unittest.main()
