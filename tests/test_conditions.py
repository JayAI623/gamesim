import copy
import unittest

from gamesim import utility as ut
from gamesim.conditions import (apply_patch, coverage_check, is_prefix, patch_within, pointer_get,
                                       required_paths, sort_conditions)
from tests.helpers import fixture_entities, small_entities


def full_conditions(ents):
    """A valid, non-overlapping, fully covering condition list (the shape conditions.md asks for)."""
    out, n = [], 0

    def add(grp, paths, conf="中"):
        nonlocal n
        n += 1
        out.append({"cid": f"c{n:02d}", "grp": grp, "sentence": f"条件 {n}：关键数字 0.{n}", "paths": paths,
                    "sources": [], "confidence": conf})

    for k in range(len(ents["variables"])):
        add("global", [f"/variables/{k}/current"], "高")
    add("global", ["/action_menu"])
    add("global", ["/periods"])
    for i, e in enumerate(ents["entities"]):
        g = e["codename"]
        add(g, [f"/entities/{i}/utility"], "低")
        add(g, [f"/entities/{i}/red_lines"])
        add(g, [f"/entities/{i}/bdm"] + ([f"/entities/{i}/capabilities"] if "capabilities" in e else []))
        add(g, [f"/entities/{i}/private_info"], "低")
    return out


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.ents = small_entities()
        self.conds = full_conditions(self.ents)

    def test_valid_list_passes(self):
        self.assertEqual(coverage_check(self.ents, self.conds), [])
        self.assertEqual(coverage_check(self.ents, {"conditions": self.conds}), [])

    def test_fixture_entities_pass(self):
        ents = fixture_entities()
        self.assertEqual(coverage_check(ents, full_conditions(ents)), [])

    def test_fine_grained_paths_also_cover(self):
        conds = [c for c in self.conds if c["paths"] != ["/entities/0/bdm", "/entities/0/capabilities"]]
        conds.append({"cid": "c90", "grp": "Alpha", "sentence": "甲：立场 30、能力 50", "confidence": "高",
                      "sources": [], "paths": ["/entities/0/bdm/position", "/entities/0/bdm/capability",
                                               "/entities/0/capabilities"]})
        conds.append({"cid": "c91", "grp": "Alpha", "sentence": "甲：重视度 0.8、风险 0", "confidence": "中",
                      "sources": [], "paths": ["/entities/0/bdm/salience", "/entities/0/bdm/risk"]})
        self.assertEqual(coverage_check(self.ents, conds), [])

    def test_missing_goals(self):
        conds = [c for c in self.conds if c["paths"] != ["/entities/1/utility"]]
        err = coverage_check(self.ents, conds)
        self.assertTrue(any("/entities/1/utility/0" in e and "乙" in e for e in err), err)

    def test_missing_bdm_param(self):
        conds = copy.deepcopy(self.conds)
        for c in conds:
            if c["paths"][0] == "/entities/0/bdm":
                c["paths"] = ["/entities/0/bdm/position", "/entities/0/bdm/capability", "/entities/0/bdm/salience",
                              "/entities/0/capabilities"]
        err = coverage_check(self.ents, conds)
        self.assertEqual(err, ["没有条件覆盖 /entities/0/bdm/risk（甲 bdm.risk）"])

    def test_path_must_exist(self):
        conds = copy.deepcopy(self.conds)
        conds[0]["paths"].append("/variables/9/current")
        err = coverage_check(self.ents, conds)
        self.assertIn("c01: path /variables/9/current 在 entities.json 里不存在", err)

    def test_same_path_in_two_conditions(self):
        conds = copy.deepcopy(self.conds)
        conds[1]["paths"].append("/variables/0/current")
        self.assertTrue(any("同时出现在 c01 和 c02" in e for e in coverage_check(self.ents, conds)))

    def test_ancestor_overlap(self):
        conds = copy.deepcopy(self.conds)
        conds.append({"cid": "c99", "grp": "Alpha", "sentence": "甲：风险 0", "paths": ["/entities/0/bdm/risk"],
                      "sources": [], "confidence": "中"})
        self.assertTrue(any("重叠" in e for e in coverage_check(self.ents, conds)))

    def test_shape_errors(self):
        conds = copy.deepcopy(self.conds)
        conds[0]["sentence"] = "长" * 61
        conds[1]["confidence"] = "很高"
        conds[2]["cid"] = "x3"
        conds[3]["cid"] = conds[4]["cid"]
        conds[5]["grp"] = "Zulu"
        conds[6]["paths"] = ["entities/0"]
        err = "\n".join(coverage_check(self.ents, conds))
        for frag in ("超过 60 字", "confidence 必须是", "cid 必须形如", "cid 重复", "grp 'Zulu'", "不是 JSON Pointer"):
            self.assertIn(frag, err)

    def test_group_scoping(self):
        conds = copy.deepcopy(self.conds)
        for c in conds:
            if c["paths"] == ["/entities/1/private_info"]:
                c["grp"] = "Alpha"
        self.assertTrue(any("不在 /entities/0 下" in e for e in coverage_check(self.ents, conds)))
        conds = copy.deepcopy(self.conds)
        for c in conds:
            if c["paths"] == ["/entities/1/private_info"]:
                c["grp"] = "global"
        self.assertTrue(any("grp=global" in e for e in coverage_check(self.ents, conds)))

    def test_too_wide(self):
        conds = copy.deepcopy(self.conds)
        conds[0]["paths"] = ["/entities"]
        conds[0]["grp"] = "global"
        self.assertTrue(any("太宽" in e or "grp=global" in e for e in coverage_check(self.ents, conds)))

    def test_required_paths(self):
        req = required_paths(self.ents)
        for p in ("/entities/0/utility/0", "/entities/0/red_lines/0", "/entities/0/capabilities",
                  "/entities/0/private_info", "/entities/1/bdm/risk", "/variables/1/current", "/action_menu/1",
                  "/periods/0"):
            self.assertIn(p, req)

    def test_missing_periods_reported(self):
        ents = small_entities(periods=False)
        conds = [c for c in full_conditions(small_entities()) if c["paths"] != ["/periods"]]
        self.assertIn("entities.json 缺少 /periods", coverage_check(ents, conds))

    def test_sort_low_first_per_group(self):
        s = sort_conditions(self.conds)
        alpha = [c["confidence"] for c in s if c["grp"] == "Alpha"]
        self.assertEqual(alpha, ["低", "低", "中", "中"])
        self.assertEqual(s[0]["grp"], "global")


class TestPatch(unittest.TestCase):
    def test_pointer_and_prefix(self):
        doc = {"a/b": {"m~n": [1, 2]}}
        self.assertEqual(pointer_get(doc, "/a~1b/m~0n/1"), 2)
        self.assertTrue(is_prefix("/entities/0", "/entities/0/bdm"))
        self.assertFalse(is_prefix("/entities/1", "/entities/10/bdm"))

    def test_apply_and_lock(self):
        ents = small_entities()
        ents["entities"][0]["utility"] = [{"var": "H", "weight": 0.6}, {"var": "W", "weight": 0.4}]
        ops = [{"op": "replace", "path": "/entities/0/utility/0/weight", "value": 0.5},
               {"op": "replace", "path": "/entities/0/utility/1/weight", "value": 0.5}]
        self.assertEqual(patch_within(ops, ["/entities/0/utility"]), [])
        self.assertEqual(len(patch_within(ops, ["/entities/0/bdm"])), 2)
        new = apply_patch(ents, ops)
        self.assertEqual([t["weight"] for t in new["entities"][0]["utility"]], [0.5, 0.5])
        self.assertEqual(ents["entities"][0]["utility"][0]["weight"], 0.6)     # original untouched
        ut.check_weights(new["entities"][0])
        bad = apply_patch(ents, ops[:1])
        with self.assertRaises(ValueError):
            ut.check_weights(bad["entities"][0])

    def test_add_remove_and_bad_path(self):
        doc = {"xs": [1, 2]}
        self.assertEqual(apply_patch(doc, [{"op": "add", "path": "/xs/-", "value": 3}])["xs"], [1, 2, 3])
        self.assertEqual(apply_patch(doc, [{"op": "remove", "path": "/xs/0"}])["xs"], [2])
        with self.assertRaises(KeyError):
            apply_patch(doc, [{"op": "replace", "path": "/xs/5", "value": 0}])


if __name__ == "__main__":
    unittest.main()
