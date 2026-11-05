import json
import os
import tempfile
import unittest

from gamesim import turn
from gamesim.fsio import PathError
from tests.helpers import dump, gm_out, move, play_round, small_entities, write_entities

EVENTS = [{"id": "ev_2", "seq": 2, "text": "乙方宣布撤军 30%"}, {"id": "ev_1", "seq": 1, "text": "联合国通过决议"}]


def read(run, rel):
    return open(os.path.join(run, rel), encoding="utf-8").read()


class TestUserEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.run_dir = os.path.join(self.base, "run_1")
        write_entities(self.run_dir, small_entities({"blind_dice": True}, rounds=3))
        turn.cmd_init(self.run_dir, seed_id="gs/run_1")
        self.events = os.path.join(self.base, "user_events_frozen.json")
        dump({"events": EVENTS}, self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def test_events_frozen_and_rendered_everywhere(self):
        run = self.run_dir
        res = turn.cmd_prep(run, 1, "stdout", self.events)
        self.assertEqual(res["events"], ["ev_1", "ev_2"])                      # sorted by seq
        ue = json.load(open(os.path.join(run, "turns/r01/user_events.json")))
        self.assertEqual([e["id"] for e in ue["events"]], ["ev_1", "ev_2"])
        dice = json.load(open(os.path.join(run, "turns/r01/dice.json")))
        self.assertEqual(set(dice), {"甲", "乙", "_exo"})                       # events are never rolled
        p = read(run, "turns/r01/prompt_Alpha.md")
        self.assertIn("## 本轮新消息（来源: 用户）", p)
        self.assertIn("联合国通过决议（来源: 用户）", p)
        for c in ("Alpha", "Bravo"):
            dump(move(), os.path.join(run, f"turns/r01/move_{c}.json"))
        turn.cmd_gm(run, 1, "stdout")
        g1 = read(run, "turns/r01/prompt_gm_commit.md")
        self.assertIn("## 已发生事件（来源: 用户，不掷骰）", g1)
        self.assertIn("[ev_2] 乙方宣布撤军 30%（来源: 用户）", g1)
        dump({"probs": {"甲": {"p_gm": 0.5}, "乙": {"p_gm": 0.5}}}, os.path.join(run, "turns/r01/gm_commit.json"))
        turn.cmd_roll(run, 1, "stdout")
        g2 = read(run, "turns/r01/prompt_gm.md")
        self.assertIn("已发生事件（来源: 用户，不掷骰）", g2)
        self.assertIn("不要掷骰", g2)
        oc = json.load(open(os.path.join(run, "turns/r01/outcomes.json")))
        self.assertEqual(oc["exogenous"], [])                                    # not turned into dice events
        dump(gm_out(), os.path.join(run, "turns/r01/gm.json"))
        commit = turn.cmd_apply(run, 1)
        self.assertEqual(commit["events_used"], ["ev_1", "ev_2"])
        cj = json.load(open(os.path.join(run, "turns/r01/commit.json")))
        self.assertEqual(cj["events_used"], ["ev_1", "ev_2"])
        rnd = json.load(open(os.path.join(run, "rounds/round_01.json")))
        self.assertEqual([e["id"] for e in rnd["user_events"]], ["ev_1", "ev_2"])
        hist = json.load(open(os.path.join(run, "state.json")))["history"]
        self.assertEqual(hist[0]["events_used"], ["ev_1", "ev_2"])

    def test_no_events_means_no_sections(self):
        turn.cmd_prep(self.run_dir, 1, "stdout")
        self.assertNotIn("本轮新消息", read(self.run_dir, "turns/r01/prompt_Alpha.md"))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "turns/r01/user_events.json")))

    def test_used_events_are_not_reapplied(self):
        play_round(self.run_dir, 1, gm_out(), events=self.events)
        more = os.path.join(self.base, "batch2.json")
        dump([*EVENTS, {"id": "ev_3", "seq": 3, "text": "选举结果公布"}], more)
        res = turn.cmd_prep(self.run_dir, 2, "stdout", more)
        self.assertEqual(res["events"], ["ev_3"])

    def test_reprep_keeps_frozen_batch_and_refuses_changes_after_moves(self):
        run = self.run_dir
        turn.cmd_prep(run, 1, "stdout", self.events)
        self.assertEqual(turn.cmd_prep(run, 1, "stdout")["events"], ["ev_1", "ev_2"])   # reused
        dump(move(), os.path.join(run, "turns/r01/move_Alpha.json"))
        other = os.path.join(self.base, "other.json")
        dump([{"id": "ev_9", "seq": 9, "text": "别的事"}], other)
        with self.assertRaises(turn.StepError):
            turn.cmd_prep(run, 1, "stdout", other)
        turn.cmd_prep(run, 1, "stdout", self.events)                                   # same batch is fine

    def test_three_runs_get_the_same_batch(self):
        prompts = []
        for k in (1, 2, 3):
            run = os.path.join(self.base, f"run_{k}")
            if k > 1:
                write_entities(run, small_entities({"blind_dice": True}, rounds=3))
                turn.cmd_init(run, seed_id=f"gs/run_{k}")
            turn.cmd_prep(run, 1, "stdout", self.events)
            prompts.append(json.load(open(os.path.join(run, "turns/r01/user_events.json")))["events"])
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(prompts[1], prompts[2])

    def test_bad_events_rejected(self):
        bad = os.path.join(self.base, "bad.json")
        dump([{"seq": 1, "text": "no id"}], bad)
        with self.assertRaises(turn.SchemaError):
            turn.cmd_prep(self.run_dir, 1, "stdout", bad)

    def test_events_file_must_be_inside_gamesim_dir(self):
        with tempfile.TemporaryDirectory() as other:
            f = os.path.join(other, "ev.json")
            dump(EVENTS, f)
            with self.assertRaises(PathError):
                turn.cmd_prep(self.run_dir, 1, "stdout", f)


if __name__ == "__main__":
    unittest.main()
