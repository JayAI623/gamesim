"""Atomic writes, write order round -> state -> private -> commit.json, idempotent replay."""
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from gamesim import fsio, status, turn
from tests.helpers import dump, gm_out, move, play_round, small_entities, write_entities


def sha(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


class Crash(Exception):
    pass


class TestCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = os.path.join(self.tmp.name, "run_1")
        write_entities(self.run_dir, small_entities(rounds=3))
        turn.cmd_init(self.run_dir, seed_id="gs/run_1")

    def tearDown(self):
        self.tmp.cleanup()

    def stage_round(self, r, gm=None):
        """Everything up to (not including) apply."""
        run = self.run_dir
        turn.cmd_prep(run, r, "stdout")
        for c in ("Alpha", "Bravo"):
            dump(move(diary=f"r{r}{c}"), os.path.join(run, f"turns/r{r:02d}/move_{c}.json"))
        turn.cmd_gm(run, r, "stdout")
        dump(gm or gm_out(), os.path.join(run, f"turns/r{r:02d}/gm.json"))

    def logs(self):
        return {c: json.load(open(os.path.join(self.run_dir, "private", f"{c}.json")))["log"] for c in ("Alpha", "Bravo")}

    def test_commit_json_contents_and_sha1(self):
        play_round(self.run_dir, 1, gm_out(), attempt=3)
        c = json.load(open(os.path.join(self.run_dir, "turns/r01/commit.json")))
        self.assertEqual(c["attempt"], 3)
        self.assertEqual(c["events_used"], [])
        self.assertEqual(list(c["sha1"]), ["rounds/round_01.json", "state.json", "private/Alpha.json",
                                           "private/Bravo.json"])                   # write order
        for rel, h in c["sha1"].items():
            self.assertEqual(sha(os.path.join(self.run_dir, rel)), h, rel)

    def test_apply_twice_is_noop(self):
        play_round(self.run_dir, 1, gm_out())
        before = {p: sha(os.path.join(self.run_dir, p)) for p in ("state.json", "private/Alpha.json")}
        again = turn.cmd_apply(self.run_dir, 1)
        self.assertTrue(again["already_committed"])
        self.assertEqual({p: sha(os.path.join(self.run_dir, p)) for p in before}, before)
        self.assertEqual(len(self.logs()["Alpha"]), 1)

    def crash_after(self, n_writes):
        """Run apply(2) but crash after the n-th JSON write; then recover with a clean apply."""
        play_round(self.run_dir, 1, gm_out(H=81))
        self.stage_round(2, gm_out(H=82))
        real, count = turn.jdump, {"n": 0}

        def flaky(obj, path):
            if count["n"] >= n_writes:
                raise Crash(str(path))
            count["n"] += 1
            return real(obj, path)

        with mock.patch.object(turn, "jdump", flaky):
            with self.assertRaises(Crash):
                turn.cmd_apply(self.run_dir, 2)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "turns/r02/commit.json")))
        self.assertEqual(turn.committed_rounds(self.run_dir), [1])
        self.assertEqual(status.next_step(self.run_dir), {"step": "apply", "round": 2})
        with self.assertRaises(turn.StepError):
            turn.cmd_prep(self.run_dir, 3)                        # round 2 is not committed yet
        res = turn.cmd_apply(self.run_dir, 2)
        self.assertFalse(res["already_committed"])
        st = json.load(open(os.path.join(self.run_dir, "state.json")))
        self.assertEqual(st["round"], 2)
        self.assertEqual([h["state_vars"]["H"] for h in st["history"]], [81, 82])
        self.assertEqual({c: [lg["round"] for lg in lg_] for c, lg_ in self.logs().items()},
                         {"Alpha": [1, 2], "Bravo": [1, 2]})       # no duplicated diary entries

    def test_crash_after_round_file(self):
        self.crash_after(1)

    def test_crash_after_state(self):
        self.crash_after(2)

    def test_crash_between_private_files(self):
        self.crash_after(3)

    def test_crash_before_commit(self):
        self.crash_after(4)

    def test_half_written_state_is_rebuilt(self):
        play_round(self.run_dir, 1, gm_out(H=81))
        with open(os.path.join(self.run_dir, "state.json"), "w") as f:
            f.write('{"round": 1, "public_st')                          # torn file (pre-atomic writer)
        turn.cmd_prep(self.run_dir, 2, "stdout")
        st = json.load(open(os.path.join(self.run_dir, "state.json")))
        self.assertEqual((st["round"], st["state_vars"]["H"]), (1, 81))

    def test_state_ahead_of_commits_is_rebuilt(self):
        play_round(self.run_dir, 1, gm_out(H=81))
        dump({"round": 2, "public_state": "bogus", "last_round_public": "", "state_vars": {"H": 1, "W": 1},
              "history": []}, os.path.join(self.run_dir, "state.json"))
        turn.cmd_prep(self.run_dir, 2, "stdout")
        st = json.load(open(os.path.join(self.run_dir, "state.json")))
        self.assertEqual((st["round"], st["public_state"]), (1, "公开"))

    def test_tampered_committed_round_detected(self):
        play_round(self.run_dir, 1, gm_out())
        self.stage_round(2)
        p = os.path.join(self.run_dir, "rounds/round_01.json")
        rec = json.load(open(p))
        rec["gm"]["state_vars"]["H"] = 0
        dump(rec, p)
        with self.assertRaises(turn.StepError):
            turn.cmd_apply(self.run_dir, 2)

    def test_status_done_needs_commit(self):
        for r in (1, 2, 3):
            play_round(self.run_dir, r, gm_out())
        self.assertEqual(status.next_step(self.run_dir), {"step": "done", "round": 3})
        os.remove(os.path.join(self.run_dir, "turns/r03/commit.json"))
        self.assertEqual(status.next_step(self.run_dir), {"step": "apply", "round": 3})
        turn.cmd_apply(self.run_dir, 3)
        self.assertEqual(status.next_step(self.run_dir)["step"], "done")

    def test_out_of_order_apply(self):
        self.stage_round(1)
        with self.assertRaises(turn.StepError):
            turn.cmd_apply(self.run_dir, 2)

    def test_init_refuses_after_commits(self):
        play_round(self.run_dir, 1, gm_out())
        with self.assertRaises(turn.StepError):
            turn.cmd_init(self.run_dir)


class TestAtomicWrite(unittest.TestCase):
    def test_failed_rename_keeps_old_file_and_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            fsio.jdump({"v": 1}, p)
            with mock.patch.object(fsio.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    fsio.jdump({"v": 2}, p)
            self.assertEqual(json.load(open(p)), {"v": 1})
            self.assertEqual(os.listdir(d), ["x.json"])

    def test_returns_sha1_of_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            self.assertEqual(fsio.jdump({"a": "中"}, p), sha(p))

    def test_fsync_called(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(fsio.os, "fsync", wraps=os.fsync) as fs:
                fsio.jdump({"v": 1}, os.path.join(d, "x.json"))
            self.assertGreaterEqual(fs.call_count, 1)


if __name__ == "__main__":
    unittest.main()
