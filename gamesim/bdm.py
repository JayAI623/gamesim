#!/usr/bin/env python3
"""Simplified Bueno de Mesquita expected-utility model (single issue axis 0-100).

Follows the structure reconstructed by Scholz, Calbert & Smith (2011) and the
dmasad/BDM_DecisionModel_Replication port, with two simplifications:
  * complete information (i uses j's true utility when classifying offers);
  * the status-quo term is U_sq only (no Q/T "better/worse" lottery).

Usage:
  python3 bdm.py input.json -o output.json [--md output.md] [--rounds 10] [--markdown]

-o is required (no shell redirection). Outputs must resolve inside the input file's directory (the run dir)
and must not go through a symlink; they are written atomically.

input.json:
  {"issue": {"low": "label at 0", "high": "label at 100"},
   "actors": [{"name": "A", "position": 20, "capability": 50,
               "salience": 0.8, "risk": 0.0}, ...]}
  position 0-100, capability > 0, salience 0-1,
  risk -1 (risk averse) .. +1 (risk acceptant).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass

try:
    from .fsio import PathError, atomic_write_text, ensure_inside, jdump
except ImportError:  # run as a script
    from fsio import PathError, atomic_write_text, ensure_inside, jdump

AXIS = 100.0


@dataclass
class Actor:
    name: str
    position: float
    capability: float
    salience: float
    risk: float = 0.0

    def validate(self) -> None:
        if not 0 <= self.position <= AXIS:
            raise ValueError(f"{self.name}: position must be in [0,100]")
        if self.capability <= 0:
            raise ValueError(f"{self.name}: capability must be > 0")
        if not 0 <= self.salience <= 1:
            raise ValueError(f"{self.name}: salience must be in [0,1]")
        if not -1 <= self.risk <= 1:
            raise ValueError(f"{self.name}: risk must be in [-1,1]")


def risk_exponent(r: float) -> float:
    """r=-1 -> 2 (risk averse), r=0 -> 1, r=+1 -> 0.5 (risk acceptant)."""
    return (1 - r / 3) / (1 + r / 3)


def vote(k: Actor, xi: float, xj: float) -> float:
    """Actor k's weighted vote for position xi over xj (positive = prefers xi)."""
    return k.capability * k.salience * (abs(k.position - xj) - abs(k.position - xi)) / AXIS


def median_position(actors: list[Actor]) -> float:
    """Candidate position with the largest total pairwise vote margin."""
    best_pos, best_score = actors[0].position, float("-inf")
    for a in actors:
        score = sum(vote(k, a.position, b.position) for b in actors for k in actors)
        if score > best_score:
            best_pos, best_score = a.position, score
    return best_pos


def weighted_mean(actors: list[Actor]) -> float:
    """Capability x salience weighted mean position (smoother forecast than the median)."""
    w = [a.capability * a.salience for a in actors]
    return sum(wi * a.position for wi, a in zip(w, actors)) / sum(w) if sum(w) else 0.0


def prob_success(actors: list[Actor], i: Actor, j: Actor) -> float:
    """Probability that i prevails over j, from third-party weighted votes."""
    votes = [vote(k, i.position, j.position) for k in actors]
    denom = sum(abs(v) for v in votes)
    if denom == 0:
        return 0.5
    return sum(v for v in votes if v > 0) / denom


def expected_utility(actors: list[Actor], i: Actor, j: Actor) -> float:
    """i's expected utility of challenging j, relative to the status quo."""
    R = risk_exponent(i.risk)
    d = abs(i.position - j.position) / AXIS
    u_s = 2 - 4 * (0.5 - 0.5 * d) ** R
    u_f = 2 - 4 * (0.5 + 0.5 * d) ** R
    u_sq = 2 - 4 * 0.5 ** R
    p = prob_success(actors, i, j)
    return j.salience * (p * u_s + (1 - p) * u_f) + (1 - j.salience) * u_s - u_sq


def classify(e_ij: float, e_ji: float) -> str:
    if e_ij > 0 and e_ji > 0:
        return "conflict" if e_ij > e_ji else "conflict_lost"
    if e_ij > 0 > e_ji:
        return "compromise" if abs(e_ij) > abs(e_ji) else "capitulation"
    return "none"


def step(actors: list[Actor]) -> tuple[list[Actor], list[dict]]:
    """One round of offers. Each actor accepts the offer needing the smallest move."""
    offers: dict[str, list[dict]] = {a.name: [] for a in actors}
    for i in actors:
        for j in actors:
            if i is j or i.position == j.position:
                continue
            e_ij = expected_utility(actors, i, j)
            e_ji = expected_utility(actors, j, i)
            kind = classify(e_ij, e_ji)
            if kind in ("conflict", "capitulation"):
                new = i.position
            elif kind == "compromise":
                new = j.position + (i.position - j.position) * abs(e_ji / e_ij)
            else:
                continue
            offers[j.name].append({"from": i.name, "to": j.name, "type": kind,
                                   "new_position": round(new, 2),
                                   "eu_from": round(e_ij, 4), "eu_to": round(e_ji, 4)})
    accepted, new_actors = [], []
    for a in actors:
        if offers[a.name]:
            best = min(offers[a.name], key=lambda o: abs(o["new_position"] - a.position))
            accepted.append(best)
            new_actors.append(Actor(a.name, best["new_position"], a.capability, a.salience, a.risk))
        else:
            new_actors.append(Actor(**asdict(a)))
    return new_actors, accepted


def run(actors: list[Actor], rounds: int = 10, tol: float = 0.5) -> dict:
    for a in actors:
        a.validate()
    if len(actors) < 2:
        raise ValueError("need at least 2 actors")
    history = [{"round": 0, "median": median_position(actors), "mean": round(weighted_mean(actors), 2),
                "positions": {a.name: a.position for a in actors}, "accepted": []}]
    current = actors
    for r in range(1, rounds + 1):
        nxt, accepted = step(current)
        moved = max(abs(a.position - b.position) for a, b in zip(current, nxt))
        current = nxt
        history.append({"round": r, "median": median_position(current),
                        "mean": round(weighted_mean(current), 2),
                        "positions": {a.name: a.position for a in current},
                        "accepted": accepted})
        if moved < tol:
            break
    return {"final_median": history[-1]["median"], "final_mean": history[-1]["mean"],
            "rounds_run": len(history) - 1,
            "history": history}


def to_markdown(result: dict, issue: dict | None = None) -> str:
    names = list(result["history"][0]["positions"])
    lines = []
    if issue:
        lines.append(f"议题轴：0 = {issue.get('low', '')}，100 = {issue.get('high', '')}\n")
    lines.append(f"**BDM 预测落点：中位 {result['final_median']:.1f} / 加权均值 {result['final_mean']:.1f}**"
                 f"（跑了 {result['rounds_run']} 轮）\n")
    lines.append("| 轮 | 中位 | 加权均值 | " + " | ".join(names) + " |")
    lines.append("|---|---|---|" + "---|" * len(names))
    for h in result["history"]:
        row = " | ".join(f"{h['positions'][n]:.1f}" for n in names)
        lines.append(f"| {h['round']} | {h['median']:.1f} | {h['mean']:.1f} | {row} |")
    return "\n".join(lines)


def load_actors(data: dict) -> list[Actor]:
    """Accept bdm.py format {"actors": [...]} or game-sim entities.json {"entities": [{"name", "bdm": {...}}]}."""
    keys = ("position", "capability", "salience", "risk")
    if "actors" in data:
        rows = [{"name": a["name"], **{k: a[k] for k in keys if k in a}} for a in data["actors"]]
    else:
        rows = [{"name": e["name"], **{k: e["bdm"][k] for k in keys if k in e["bdm"]}} for e in data["entities"]]
    return [Actor(**r) for r in rows]


def write_outputs(input_path: str, result: dict, output: str, md: str | None = None,
                  issue: dict | None = None) -> None:
    root = os.path.dirname(os.path.abspath(input_path))
    jdump(result, ensure_inside(root, output))
    if md:
        atomic_write_text(ensure_inside(root, md), to_markdown(result, issue) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--md", help="also write the markdown table here")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--markdown", action="store_true", help="print markdown table")
    args = ap.parse_args(argv)
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    actors = load_actors(data)
    result = run(actors, rounds=args.rounds)
    result["issue"] = data.get("issue")
    try:
        write_outputs(args.input, result, args.output, args.md, data.get("issue"))
    except PathError as ex:
        print(f"ERROR {ex}", file=sys.stderr)
        return 2
    print(to_markdown(result, data.get("issue")) if args.markdown
          else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
