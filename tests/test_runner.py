"""runner + fork with the fake claude: 2 runs × 2 rounds, blind dice, a branch swap after round 1, one party that
always refuses (-> raw outputs kept, fallback move), then a stress fork."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from gamesim import fork, runner, turn
from gamesim.fsio import jload
from tests.fake_env import calls, fake_claude
from tests.helpers import dump, small_entities

SWAP = {"after_round": 1, "assign": {"1": "A", "2": "B"}, "p": {"A": 0.75, "B": 0.25},
        "branches": {"A": {"patch": [{"op": "replace", "path": "/entities/0/utility/0/direction", "value": "-"}],
                           "event": "分支 A：甲方换届"},
                     "B": {"patch": [], "event": "分支 B：维持原状"}}}


class TestClaudeArgv(unittest.TestCase):
    def test_argv_is_exact_and_bin_overridable(self):
        with mock.patch.dict(os.environ, {"GAMESIM_CLAUDE_BIN": "claude"}):
            self.assertEqual(runner.claude_argv("sonnet"), [
                "claude", "-p", "--output-format", "text", "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--disable-slash-commands", "--tools", "", "--model", "sonnet"])
        with mock.patch.dict(os.environ, {"GAMESIM_CLAUDE_BIN": "python3 /x/fake.py"}):
            self.assertEqual(runner.claude_argv("opus")[:3], ["python3", "/x/fake.py", "-p"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GAMESIM_CLAUDE_BIN", None)
            self.assertEqual(runner.claude_argv("m")[0], "claude")

    def test_call_runs_in_empty_temp_dir(self):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            seen["listing"] = os.listdir(kw["cwd"])
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch("subprocess.run", fake_run):
            self.assertEqual(runner.call_claude("PROMPT", "sonnet", 5), "ok")
        self.assertEqual((seen["input"], seen["listing"], seen["timeout"]), ("PROMPT", [], 5))
        self.assertFalse(os.path.exists(seen["cwd"]))


class TestRunnerAndFork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.base = os.path.join(cls.tmp, "base")
        src = os.path.join(cls.tmp, "entities.json")
        dump(small_entities({"blind_dice": True, "reference_class": True}), src)
        swap = os.path.join(cls.tmp, "swap.json")
        dump(SWAP, swap)
        cls.log = os.path.join(cls.tmp, "calls.log")
        with fake_claude(refuse="乙", log_path=cls.log):
            cls.res = runner.run_all(cls.base, src, 2, swap_file=swap, tries=1, parallel=4)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_dir(self, k):
        return os.path.join(self.base, f"run_{k}")

    def test_runs_complete_with_distinct_seeds(self):
        for k in (1, 2):
            self.assertEqual(turn.committed_rounds(self.run_dir(k)), [1, 2])
        self.assertNotEqual(jload(os.path.join(self.run_dir(1), "run.json"))["seed"],
                            jload(os.path.join(self.run_dir(2), "run.json"))["seed"])
        self.assertEqual(self.res["k"]["n"], 2)
        self.assertTrue(self.res["k"]["complete"])
        self.assertTrue(os.path.exists(os.path.join(self.base, "k.md")))

    def test_refusals_are_kept_and_fall_back(self):
        fb = runner.fallback_stats(self.base)
        for k in (1, 2):
            self.assertEqual(fb[f"run_{k}"]["fallback"], 2)              # 乙, both rounds
            self.assertEqual(fb[f"run_{k}"]["by_entity"], {"乙": 2})
            self.assertEqual(fb[f"run_{k}"]["raw_failures"], 4)          # 2 rounds × (1 + tries=1)
        raw = open(os.path.join(self.run_dir(1), "turns", "r01", "raw_Bravo_0.txt"), encoding="utf-8").read()
        self.assertIn("不能", raw)
        m = jload(os.path.join(self.run_dir(1), "turns", "r01", "move_Bravo.json"))
        self.assertEqual((m["fallback"], m["chosen"]["escalation"]), (True, 1))
        self.assertEqual(jload(os.path.join(self.base, "fallback_stats.json"))["run_1"]["fallback"], 2)

    def test_call_counts(self):
        c = calls(self.log)
        # roles: 2 runs × 2 rounds × (甲 1 call + 乙 2 refused calls); GM: commit + resolve per run-round
        self.assertEqual(c, {"role": 12, "gm_commit": 4, "gm": 4})

    def test_swap_patches_and_announces(self):
        e1 = jload(os.path.join(self.run_dir(1), "entities.json"))
        self.assertEqual(e1["entities"][0]["utility"][0]["direction"], "-")
        pre = jload(os.path.join(self.run_dir(1), "entities.pre_swap.json"))
        self.assertEqual(pre["entities"][0]["utility"][0]["direction"], "+")
        self.assertEqual(jload(os.path.join(self.run_dir(2), "branch.json"))["side"], "B")
        ev = jload(os.path.join(self.run_dir(2), "turns", "r02", "user_events.json"))["events"]
        self.assertEqual([x["text"] for x in ev], ["分支 B：维持原状"])
        p = open(os.path.join(self.run_dir(1), "turns", "r02", "prompt_Alpha.md"), encoding="utf-8").read()
        self.assertIn("分支 A：甲方换届（来源: 用户）", p)
        r1 = open(os.path.join(self.run_dir(1), "turns", "r01", "prompt_Alpha.md"), encoding="utf-8").read()
        self.assertNotIn("分支 A", r1)
        self.assertEqual(runner.run_weights(self.base), {"run_1": 0.75, "run_2": 0.25})

    def test_rerun_is_a_no_op(self):
        log2 = os.path.join(self.tmp, "calls2.log")
        with fake_claude(log_path=log2):
            runner.run_all(self.base, None, 2, tries=1)
        self.assertEqual(calls(log2), {})

    def test_branch_draw_without_assign(self):
        sw = {**SWAP, "assign": {}}
        sides = {runner.branch_of(sw, k, 7)[0] for k in range(1, 40)}
        self.assertEqual(sides, {"A", "B"})
        self.assertEqual(runner.branch_of(sw, 3, 7), runner.branch_of(sw, 3, 7))

    def test_fork_after_swap_keeps_branch_event_and_adds_stress(self):
        ev = os.path.join(self.tmp, "stress.json")
        dump([{"id": "stress_1", "seq": 1, "text": "【压力测试】突发危机"}], ev)
        dst = os.path.join(self.base, "run_3")
        with fake_claude():
            res = fork.fork_and_play(self.run_dir(2), dst, 1, ev, tries=1)
        self.assertEqual(res["events"], ["branch_B_1", "stress_1"])
        self.assertEqual(turn.committed_rounds(dst), [1, 2])
        self.assertEqual(jload(os.path.join(dst, "rounds", "round_01.json")),
                         jload(os.path.join(self.run_dir(2), "rounds", "round_01.json")))
        ue = jload(os.path.join(dst, "turns", "r02", "user_events.json"))["events"]
        self.assertEqual([x["id"] for x in ue], ["branch_B_1", "stress_1"])
        self.assertEqual(jload(os.path.join(dst, "fork.json"))["src"], "run_2")
        with self.assertRaises(FileExistsError):
            fork.fork(self.run_dir(2), dst, 1)
        with self.assertRaises(ValueError):
            fork.fork(self.run_dir(2), os.path.join(self.base, "run_9"), 5)

    def test_fork_before_swap_restores_entities(self):
        dst = os.path.join(self.base, "run_5")
        info = fork.fork(self.run_dir(1), dst, 0, reseed=99)
        self.assertEqual(turn.committed_rounds(dst), [])
        self.assertFalse(os.path.exists(os.path.join(dst, "branch.json")))
        self.assertEqual(jload(os.path.join(dst, "entities.json"))["entities"][0]["utility"][0]["direction"], "+")
        self.assertEqual(jload(os.path.join(dst, "run.json"))["seed"], 99)
        self.assertIsNone(info["events_file"])
        st = jload(os.path.join(dst, "state.json"))
        self.assertEqual((st["round"], st["history"]), (0, []))
        shutil.rmtree(dst)


if __name__ == "__main__":
    unittest.main()
