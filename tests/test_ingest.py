"""stdout mode: model stdout text -> move_<code>.json / gm_commit.json / gm.json (+ setup outputs)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gamesim import turn
from gamesim.fsio import extract_json
from gamesim.ingest import (IngestError, fallback_move, ingest_round, ingest_setup, write_fallback_move)
from tests.helpers import HORMUZ_TARGET, gm_out, move, small_entities, write_entities
from tests.test_conditions import full_conditions

REPO = Path(__file__).resolve().parents[1]


def fenced(obj, prose="好的，这是我的决定："):
    return f"{prose}\n```json\n{json.dumps(obj, ensure_ascii=False)}\n```\n"


class TestExtract(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json("```json\n{\"a\": 1}\n```"), {"a": 1})
        self.assertEqual(extract_json("说明\n```\n{\"a\": 1}\n```"), {"a": 1})
        self.assertEqual(extract_json('前言 {"a": {"b": 2}} 后记'), {"a": {"b": 2}})
        self.assertEqual(extract_json("```text\nnot json\n```\n```json\n[1]\n```"), [1])
        with self.assertRaises(ValueError):
            extract_json("no json at all")


class TestIngestRound(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = self.tmp.name
        write_entities(self.run_dir, small_entities({"blind_dice": True}))
        turn.cmd_init(self.run_dir)
        turn.cmd_prep(self.run_dir, 1, "stdout")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_round_from_stdout(self):
        run = self.run_dir
        for c in ("Alpha", "Bravo"):
            res = ingest_round(run, 1, "move", fenced(move()), c)
            self.assertTrue(res["path"].endswith(f"turns/r01/move_{c}.json"))
        turn.cmd_gm(run, 1, "stdout")
        ingest_round(run, 1, "gm_commit", fenced({"probs": {"甲": {"p_gm": 0.4}, "乙": {"p_gm": 0.6}}}))
        turn.cmd_roll(run, 1, "stdout")
        ingest_round(run, 1, "gm", fenced(gm_out(96, 20, True)))
        commit = turn.cmd_apply(run, 1)
        self.assertTrue(commit["hit"])

    def test_bad_outputs(self):
        with self.assertRaisesRegex(IngestError, "没有找到合法的 JSON"):
            ingest_round(self.run_dir, 1, "move", "I refuse", "Alpha")
        with self.assertRaisesRegex(IngestError, "chosen_index"):
            ingest_round(self.run_dir, 1, "move", fenced({"chosen": {"escalation": 2}, "position_now": 3,
                                                          "options_considered": []}), "Alpha")
        with self.assertRaisesRegex(IngestError, "顶层必须是对象"):
            ingest_round(self.run_dir, 1, "move", "[1, 2]", "Alpha")
        with self.assertRaisesRegex(IngestError, "乙"):
            ingest_round(self.run_dir, 1, "gm_commit", fenced({"probs": {"甲": {"p_gm": 0.4}}}))
        with self.assertRaisesRegex(IngestError, "p_gm"):
            ingest_round(self.run_dir, 1, "gm_commit", fenced({"probs": {"甲": {"p_gm": 4}, "乙": {"p_gm": 0.1}}}))
        g = gm_out()
        del g["agreement"]
        with self.assertRaisesRegex(IngestError, "agreement.in_effect"):
            ingest_round(self.run_dir, 1, "gm", fenced(g))
        with self.assertRaisesRegex(IngestError, "public_state"):
            ingest_round(self.run_dir, 1, "gm", fenced({"state_vars": {}, "agreement": {"in_effect": False}}))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "turns/r01/move_Alpha.json")))

    def test_fallback_move_is_valid(self):
        self.assertEqual(turn.validate_move(fallback_move("甲")), [])
        write_fallback_move(self.run_dir, 1, "Alpha", "两次都不是 JSON")
        ingest_round(self.run_dir, 1, "move", fenced(move()), "Bravo")
        res = turn.cmd_gm(self.run_dir, 1, "stdout")
        self.assertEqual(res["phase"], "commit")
        m = json.load(open(os.path.join(self.run_dir, "turns/r01/move_Alpha.json")))
        self.assertEqual((m["chosen"]["escalation"], m["fallback"]), (1, True))

    def test_cli_reads_stdin(self):
        r = subprocess.run([sys.executable, "-m", "gamesim.ingest", "round", self.run_dir, "1", "move", "Alpha"],
                           cwd=REPO, input=fenced(move()), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.strip().endswith("move_Alpha.json"))
        r = subprocess.run([sys.executable, "gamesim/ingest.py", "round", self.run_dir, "1", "move", "Bravo"],
                           cwd=REPO, input="garbage", capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("JSON", r.stdout)


class TestIngestSetup(unittest.TestCase):
    def test_entities_conditions_summary(self):
        with tempfile.TemporaryDirectory() as d:
            assembled = small_entities(target=False, periods=False)
            periods = small_entities()["periods"]
            res = ingest_setup(d, "entities", fenced(assembled), target=HORMUZ_TARGET, periods=periods, rounds=2)
            ents = json.load(open(res["path"]))
            self.assertEqual(ents["target"], HORMUZ_TARGET)
            self.assertTrue(ents["settings"]["blind_dice"])
            res = ingest_setup(d, "conditions", fenced(full_conditions(ents)))
            conds = json.load(open(res["path"]))
            self.assertEqual(conds[0]["grp"], "global")
            self.assertEqual([c["confidence"] for c in conds if c["grp"] == "Alpha"][:2], ["低", "低"])
            bad = full_conditions(ents)[:-1]
            with self.assertRaisesRegex(IngestError, "private_info"):
                ingest_setup(d, "conditions", fenced(bad))
            ok = {"bottlenecks": ["乙的红线 H≤5 卡住 2/3 局"], "signposts": ["若 H 在 12-31 前 ≥ 90，命中上升"],
                  "invalidators": ["甲宣布退出谈判"]}
            ingest_setup(d, "result_summary", fenced(ok))
            with self.assertRaisesRegex(IngestError, "阈值"):
                ingest_setup(d, "result_summary", fenced({**ok, "signposts": ["若局势好转"]}))
            with self.assertRaisesRegex(IngestError, "80"):
                ingest_setup(d, "result_summary", fenced({**ok, "bottlenecks": ["长" * 81]}))

    def test_entities_rejected_with_reasons(self):
        with tempfile.TemporaryDirectory() as d:
            bad = small_entities()
            bad["entities"][0]["utility"][0]["weight"] = 0.7
            with self.assertRaisesRegex(IngestError, "权重之和"):
                ingest_setup(d, "entities", fenced(bad))
            self.assertFalse(os.path.exists(os.path.join(d, "entities.json")))


if __name__ == "__main__":
    unittest.main()
