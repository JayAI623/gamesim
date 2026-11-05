import os
import sys
import unittest

from gamesim import bdm  # noqa: E402
from gamesim.bdm import Actor  # noqa: E402


def actors3():
    return [Actor("Strong", 80, 100, 0.9), Actor("Mid", 50, 40, 0.6), Actor("Weak", 10, 10, 0.5)]


class TestBDM(unittest.TestCase):
    def test_risk_exponent(self):
        self.assertAlmostEqual(bdm.risk_exponent(0), 1)
        self.assertAlmostEqual(bdm.risk_exponent(-1), 2)
        self.assertAlmostEqual(bdm.risk_exponent(1), 0.5)

    def test_eu_zero_at_same_position(self):
        a, b = Actor("A", 40, 10, 0.5), Actor("B", 40, 20, 0.7)
        self.assertAlmostEqual(bdm.expected_utility([a, b], a, b), 0)

    def test_risk_acceptant_challenges_more(self):
        base = [Actor("A", 20, 50, 0.8), Actor("B", 80, 50, 0.8)]
        averse = [Actor("A", 20, 50, 0.8, -1), base[1]]
        acceptant = [Actor("A", 20, 50, 0.8, 1), base[1]]
        e_av = bdm.expected_utility(averse, averse[0], averse[1])
        e_ac = bdm.expected_utility(acceptant, acceptant[0], acceptant[1])
        self.assertLess(e_av, e_ac)

    def test_median_follows_dominant_actor(self):
        acts = [Actor("Big", 70, 1000, 1.0), Actor("S1", 10, 1, 0.5), Actor("S2", 30, 1, 0.5)]
        self.assertEqual(bdm.median_position(acts), 70)

    def test_prob_success_bounds(self):
        acts = actors3()
        p = bdm.prob_success(acts, acts[0], acts[2])
        self.assertGreater(p, 0.5)
        self.assertLessEqual(p, 1)

    def test_weak_actor_moves_toward_strong(self):
        res = bdm.run(actors3(), rounds=10)
        final = res["history"][-1]["positions"]
        self.assertGreater(final["Weak"], 10)
        for v in final.values():
            self.assertTrue(0 <= v <= 100)

    def test_classify(self):
        self.assertEqual(bdm.classify(1, 0.5), "conflict")
        self.assertEqual(bdm.classify(1, -0.5), "compromise")
        self.assertEqual(bdm.classify(0.5, -1), "capitulation")
        self.assertEqual(bdm.classify(-1, -1), "none")

    def test_validation(self):
        with self.assertRaises(ValueError):
            bdm.run([Actor("A", 120, 1, 0.5), Actor("B", 0, 1, 0.5)])
        with self.assertRaises(ValueError):
            bdm.run([Actor("A", 50, 1, 1.5), Actor("B", 0, 1, 0.5)])
        with self.assertRaises(ValueError):
            bdm.run([Actor("A", 50, 1, 0.5)])

    def test_weighted_mean(self):
        acts = [Actor("A", 0, 1, 1.0), Actor("B", 100, 3, 1.0)]
        self.assertAlmostEqual(bdm.weighted_mean(acts), 75)

    def test_load_actors_both_formats(self):
        a = bdm.load_actors({"actors": [{"name": "A", "position": 1, "capability": 2, "salience": 0.5}]})
        e = bdm.load_actors({"entities": [{"name": "A", "goals": [],
                                           "bdm": {"position": 1, "capability": 2, "salience": 0.5}}]})
        self.assertEqual(a, e)

    def test_converges_and_stops(self):
        res = bdm.run(actors3(), rounds=50)
        self.assertLess(res["rounds_run"], 50)


if __name__ == "__main__":
    unittest.main()
