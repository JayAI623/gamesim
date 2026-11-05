#!/usr/bin/env python3
"""Compute each player's realized utility from shared state variables.

entities.json must contain:
  "variables": [{"sym": "S", "name": "...", "lo": 0, "hi": 100, "current": 30}, ...]
  "entities":  [{"name": "...", "utility": [{"var", "weight", "direction": "+|-",
                                              "shape": "linear|concave|convex|threshold|peak",
                                              "threshold": null}],
                 "red_lines": [{"rule", "var", "op": ">=|<=|>|<", "threshold"}]}]

State = {sym: value}. Round files rounds/round_NN.json carry gm.state_vars.

Usage:
  python3 utility.py <run_dir>            # table of U per round (round 0 = current values)
  python3 utility.py <run_dir> --state '{"S": 50, "O": 90}'   # evaluate one state
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys

RED_LINE_PENALTY = -10.0
OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
       ">": lambda a, b: a > b, "<": lambda a, b: a < b}


def normalize(x: float, lo: float, hi: float) -> float:
    """Map x to [0,1], clipped."""
    if hi == lo:
        return 0.5
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def term_value(t: dict, var: dict, x: float) -> float:
    """u_k in [-1, 1]. direction '+' means more of the variable is better."""
    z = normalize(x, var["lo"], var["hi"])
    if t.get("direction", "+") == "-":
        z = 1 - z
    shape = t.get("shape", "linear")
    if shape == "concave":
        z = math.sqrt(z)
    elif shape == "convex":
        z = z * z
    elif shape == "peak":
        # inverted U: +1 at threshold (the ideal point), -1 at the farther bound
        pk = t.get("threshold")
        if pk is None:
            raise ValueError(f"peak shape needs 'threshold' (ideal point) for var {t['var']}")
        span = max(pk - var["lo"], var["hi"] - pk) or 1
        return 1 - 2 * abs(x - pk) / span
    elif shape == "threshold":
        thr = t.get("threshold")
        if thr is None:
            raise ValueError(f"threshold shape needs 'threshold' for var {t['var']}")
        good = x >= thr if t.get("direction", "+") == "+" else x <= thr
        return 1.0 if good else -1.0
    return 2 * z - 1


def utility(entity: dict, variables: dict[str, dict], state: dict[str, float]) -> dict:
    breached = []
    for rl in entity.get("red_lines", []):
        v = rl.get("var")
        if v in state and rl.get("op") in OPS and OPS[rl["op"]](state[v], rl["threshold"]):
            breached.append(rl.get("rule", v))
    total, parts = 0.0, {}
    for t in entity.get("utility", []):
        v = t["var"]
        if v not in state or v not in variables:
            continue
        u = term_value(t, variables[v], state[v])
        parts[v] = round(t["weight"] * u, 3)
        total += t["weight"] * u
    if breached:
        total = RED_LINE_PENALTY
    return {"U": round(total, 3), "parts": parts, "red_lines_breached": breached}


def check_weights(entity: dict, tol: float = 0.02) -> None:
    s = sum(t["weight"] for t in entity.get("utility", []))
    if abs(s - 1) > tol:
        raise ValueError(f"{entity['name']}: utility weights sum to {s:.3f}, expected 1")


def load(run_dir: str) -> tuple[dict, dict]:
    with open(os.path.join(run_dir, "entities.json"), encoding="utf-8") as f:
        data = json.load(f)
    variables = {v["sym"]: v for v in data["variables"]}
    return data, variables


def table(run_dir: str) -> str:
    data, variables = load(run_dir)
    ents = data["entities"]
    for e in ents:
        check_weights(e)
    states = [(0, {k: v["current"] for k, v in variables.items()})]
    for p in sorted(glob.glob(os.path.join(run_dir, "rounds", "round_*.json"))):
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        sv = r.get("gm", {}).get("state_vars")
        if sv:
            states.append((r["round"], {**states[-1][1], **sv}))
    syms = list(variables)
    lines = ["| 轮 | " + " | ".join(syms) + " | " + " | ".join(e["name"] for e in ents) + " |",
             "|---|" + "---|" * (len(syms) + len(ents))]
    for rnd, st in states:
        us = [utility(e, variables, st) for e in ents]
        cells = [f"{u['U']:+.2f}" + ("⚠" if u["red_lines_breached"] else "") for u in us]
        lines.append(f"| {rnd} | " + " | ".join(f"{st[s]:g}" for s in syms) + " | " + " | ".join(cells) + " |")
    lines.append("\n_U ∈ [−1, +1]；⚠ = 红线被突破（U = −10）_")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--state", help="JSON dict of variable values to evaluate")
    args = ap.parse_args(argv)
    if args.state:
        data, variables = load(args.run_dir)
        st = {k: v["current"] for k, v in variables.items()}
        st.update(json.loads(args.state))
        print(json.dumps({e["name"]: utility(e, variables, st) for e in data["entities"]},
                         ensure_ascii=False, indent=2))
    else:
        print(table(args.run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
