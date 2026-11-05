import copy
import unittest

from gamesim.schema import validate_entities, validate_events
from tests.helpers import fixture_entities, small_entities


def errs(data, **kw):
    return "\n".join(validate_entities(data, **kw))


class TestValidateEntities(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_entities(small_entities()), [])
        self.assertEqual(validate_entities(fixture_entities()), [])

    def test_legacy_fixture_without_target_or_periods(self):
        legacy = fixture_entities(v08=False)
        self.assertIn("缺少 target", errs(legacy))
        self.assertEqual(validate_entities(legacy, require_target=False, require_periods=False), [])

    def test_common_assembly_mistakes(self):
        cases = [
            (lambda d: d["entities"][0]["utility"][0].update(var="Z"), "不在 variables 里"),
            (lambda d: d["entities"][0]["utility"][0].update(weight=0.5), "权重之和"),
            (lambda d: d["entities"][0].update(codename="alpha"), "codename"),
            (lambda d: d["entities"][1].update(weight=0.9), "weight 之和"),
            (lambda d: d["variables"][0].update(current=150), "current"),
            (lambda d: d["entities"][0]["bdm"].update(salience=2), "salience"),
            (lambda d: d["entities"][0]["red_lines"][0].update(op="=="), "red_lines[0].op"),
            (lambda d: d["entities"][0]["utility"][0].update(shape="peak"), "需要数字 threshold"),
            (lambda d: d["periods"].pop(), "periods 必须是长度"),
            (lambda d: d["periods"][1].update(start="2026-11-01"), "重叠"),
            (lambda d: d.pop("agreement"), "没有 agreement"),
            (lambda d: d["target"].update(kind="sustained"), "min_rounds"),
            (lambda d: d["target"]["conds"][0].update(op="~"), "target.conds[0]"),
            (lambda d: d.pop("initial_public_state"), "initial_public_state"),
            (lambda d: d["entities"].pop(), "entities 必须是"),
        ]
        for mutate, frag in cases:
            d = copy.deepcopy(small_entities())
            mutate(d)
            self.assertIn(frag, errs(d), frag)

    def test_not_a_dict(self):
        self.assertEqual(validate_entities([]), ["entities.json 顶层必须是 JSON 对象"])


class TestValidateEvents(unittest.TestCase):
    def test_events(self):
        self.assertEqual(validate_events([{"id": "a", "seq": 1, "text": "x"}]), [])
        self.assertTrue(validate_events({"id": "a"}))
        e = validate_events([{"id": "a", "seq": 1, "text": "x"}, {"id": "a", "seq": "2", "text": ""}])
        self.assertEqual(len(e), 3)


if __name__ == "__main__":
    unittest.main()
