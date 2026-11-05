import json
import os
import tempfile
import unittest
from pathlib import Path

from gamesim import bdm, ingest, turn
from gamesim.fsio import PathError, check_codename, ensure_inside, safe_path
from tests.helpers import dump, move, small_entities, write_entities


class TestCodename(unittest.TestCase):
    def test_good(self):
        for c in ("Alpha", "Foxtrot", "Abc", "A" + "b" * 15):
            self.assertEqual(check_codename(c), c)

    def test_bad(self):
        for c in ("alpha", "Ab", "ALPHA", "Alpha1", "Al-pha", "../etc", "Alpha/..", "A" + "b" * 16, "", None, 3):
            with self.assertRaises(PathError, msg=repr(c)):
                check_codename(c)

    def test_bad_codename_in_entities_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as d:
            data = small_entities()
            data["entities"][1]["codename"] = "../../evil"
            write_entities(d, data)
            with self.assertRaises(PathError):
                turn.cmd_init(d)
            self.assertFalse(os.path.exists(os.path.join(d, "private")))
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(d), "evil.json")))

    def test_duplicate_codename_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            data = small_entities()
            data["entities"][1]["codename"] = "Alpha"
            write_entities(d, data)
            with self.assertRaises(PathError):
                turn.cmd_init(d)


class TestSafePath(unittest.TestCase):
    def test_dotdot_absolute_empty(self):
        with tempfile.TemporaryDirectory() as d:
            for parts in (("..", "x"), ("turns", "../../x"), ("/etc/passwd",), ("",), ("a//b",)):
                with self.assertRaises(PathError, msg=parts):
                    safe_path(d, *parts)
            self.assertEqual(safe_path(d, "turns", "r01", "x.json"), Path(d) / "turns" / "r01" / "x.json")

    def test_ensure_inside(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            ensure_inside(d, os.path.join(d, "a", "b.json"))
            with self.assertRaises(PathError):
                ensure_inside(d, os.path.join(other, "b.json"))
            with self.assertRaises(PathError):
                ensure_inside(d, os.path.join(d, "..", os.path.basename(other), "b.json"))

    def test_symlinked_dir_inside_run_rejected(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            write_entities(d, small_entities())
            turn.cmd_init(d)
            os.symlink(other, os.path.join(d, "turns"))
            with self.assertRaises(PathError):
                turn.cmd_prep(d, 1)
            self.assertEqual(os.listdir(other), [])

    def test_symlinked_file_rejected(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            write_entities(d, small_entities())
            turn.cmd_init(d)
            target = os.path.join(other, "victim.json")
            dump({"keep": True}, target)
            os.remove(os.path.join(d, "private", "Bravo.json"))
            os.symlink(target, os.path.join(d, "private", "Bravo.json"))
            with self.assertRaises(PathError):
                turn.cmd_init(d, force=True)
            self.assertEqual(json.load(open(target)), {"keep": True})

    def test_run_root_itself_may_be_a_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            write_entities(real, small_entities())
            os.symlink(real, os.path.join(d, "link"))
            turn.cmd_init(os.path.join(d, "link"))
            turn.cmd_prep(os.path.join(d, "link"), 1)
            self.assertTrue(os.path.exists(os.path.join(real, "turns", "r01", "prompt_Alpha.md")))

    def test_ingest_rejects_bad_or_unknown_codename(self):
        with tempfile.TemporaryDirectory() as d:
            write_entities(d, small_entities())
            turn.cmd_init(d)
            with self.assertRaises(PathError):
                ingest.ingest_round(d, 1, "move", json.dumps(move()), "../x")
            with self.assertRaises(PathError):
                ingest.ingest_round(d, 1, "move", json.dumps(move()), "Zulu")


class TestBdmOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        write_entities(self.d, small_entities())
        self.inp = os.path.join(self.d, "entities.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_o_required(self):
        with self.assertRaises(SystemExit):
            bdm.main([self.inp])

    def test_o_inside_run_dir(self):
        out = os.path.join(self.d, "bdm_result.json")
        self.assertEqual(bdm.main([self.inp, "-o", out, "--md", os.path.join(self.d, "bdm_result.md")]), 0)
        self.assertIn("final_median", json.load(open(out)))
        self.assertTrue(os.path.exists(os.path.join(self.d, "bdm_result.md")))

    def test_o_outside_or_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(bdm.main([self.inp, "-o", os.path.join(other, "x.json")]), 2)
            self.assertFalse(os.path.exists(os.path.join(other, "x.json")))
            self.assertEqual(bdm.main([self.inp, "-o", os.path.join(self.d, "..", "x.json")]), 2)
            os.symlink(os.path.join(other, "y.json"), os.path.join(self.d, "bdm_result.json"))
            self.assertEqual(bdm.main([self.inp, "-o", os.path.join(self.d, "bdm_result.json")]), 2)
            self.assertFalse(os.path.exists(os.path.join(other, "y.json")))


if __name__ == "__main__":
    unittest.main()
