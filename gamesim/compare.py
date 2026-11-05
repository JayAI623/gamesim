#!/usr/bin/env python3
"""Summarise a game-sim run directory into markdown metrics.

Reads:
  <run>/entities.json        {"issue": {...}, "entities": [{"name", "codename"?, "bdm": {...}}]}
  <run>/rounds/round_NN.json {"round": n, "moves": {name: move}, "gm": {...}}
  <run>/bdm_result.json      output of bdm.py (optional)

A move looks like:
  {"options_considered": [{"action", "p_success", "payoff_success", "payoff_fail", "eu"}],
   "chosen_index": 0, "chosen": {"action", "target", "escalation", "public_statement"},
   "position_now": 0-100}

Usage: python3 compare.py <run_dir> [-o metrics.md]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

try:
    from .fsio import PathError, atomic_write_text, ensure_inside
except ImportError:  # run as a script
    from fsio import PathError, atomic_write_text, ensure_inside

EU_TOL = 0.05


def load_rounds(run_dir: str) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(run_dir, "rounds", "round_*.json")))
    rounds = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            rounds.append(json.load(f))
    return rounds


def recompute_eu(opt: dict) -> float | None:
    try:
        p = float(opt["p_success"])
        return p * float(opt["payoff_success"]) + (1 - p) * float(opt["payoff_fail"])
    except (KeyError, TypeError, ValueError):
        return None


def audit_move(move: dict) -> dict:
    """Check arithmetic of self-reported EU and whether the argmax option was chosen."""
    opts = move.get("options_considered") or []
    arith_errors = 0
    eus = []
    for o in opts:
        eu = recompute_eu(o)
        eus.append(eu)
        if eu is not None and "eu" in o and abs(float(o["eu"]) - eu) > EU_TOL:
            arith_errors += 1
    idx = move.get("chosen_index")
    valid = [e for e in eus if e is not None]
    picked_argmax = None
    if valid and isinstance(idx, int) and 0 <= idx < len(eus) and eus[idx] is not None:
        picked_argmax = eus[idx] >= max(valid) - 1e-9
    return {"arith_errors": arith_errors, "n_options": len(opts), "picked_argmax": picked_argmax}


def summarise(run_dir: str) -> str:
    rounds = load_rounds(run_dir)
    if not rounds:
        return "_没有找到 rounds/round_*.json_"
    names = sorted({n for r in rounds for n in r.get("moves", {})})
    out = []

    # 1. escalation per round
    out.append("### 升级强度（0–10）\n")
    out.append("| 轮 | 最高 | 平均 | " + " | ".join(names) + " |")
    out.append("|---|---|---|" + "---|" * len(names))
    for r in rounds:
        esc = {n: r["moves"].get(n, {}).get("chosen", {}).get("escalation") for n in names}
        vals = [float(v) for v in esc.values() if isinstance(v, (int, float))]
        mx = f"{max(vals):.0f}" if vals else "-"
        mean = f"{sum(vals) / len(vals):.1f}" if vals else "-"
        cells = " | ".join("-" if esc[n] is None else str(esc[n]) for n in names)
        out.append(f"| {r['round']} | {mx} | {mean} | {cells} |")

    # 2. positions: LLM vs BDM
    bdm_path = os.path.join(run_dir, "bdm_result.json")
    bdm_hist = None
    if os.path.exists(bdm_path):
        with open(bdm_path, encoding="utf-8") as f:
            bdm_hist = json.load(f).get("history")
    out.append("\n### 议题立场（0–100）：LLM 推演 vs BDM 基线\n")
    out.append("| 轮 | " + " | ".join(names) + " |")
    out.append("|---|" + "---|" * len(names))
    for r in rounds:
        cells = []
        for n in names:
            llm = r["moves"].get(n, {}).get("position_now")
            b = None
            if bdm_hist:
                h = bdm_hist[min(r["round"], len(bdm_hist) - 1)]
                b = h["positions"].get(n)
            llm_s = "-" if llm is None else f"{float(llm):.0f}"
            b_s = "" if b is None else f" / {b:.0f}"
            cells.append(llm_s + b_s)
        out.append(f"| {r['round']} | " + " | ".join(cells) + " |")
    if bdm_hist:
        out.append("\n_单元格 = LLM / BDM_")
        last = rounds[-1]
        llm_pos = [float(m["position_now"]) for m in last["moves"].values()
                   if isinstance(m.get("position_now"), (int, float))]
        if llm_pos:
            with open(bdm_path, encoding="utf-8") as f:
                b = json.load(f)
            out.append(f"\n最终：LLM 立场均值 {sum(llm_pos) / len(llm_pos):.1f}；"
                       f"BDM 中位 {b['final_median']:.1f}，加权均值 {b.get('final_mean', float('nan')):.1f}")

    # 3. rationality audit
    out.append("\n### 理性一致性审计\n")
    out.append("| 实体 | 决策数 | 选了 EU 最高项 | EU 算术错误 |")
    out.append("|---|---|---|---|")
    for n in names:
        audits = [audit_move(r["moves"][n]) for r in rounds if n in r.get("moves", {})]
        judged = [a["picked_argmax"] for a in audits if a["picked_argmax"] is not None]
        argmax_rate = f"{sum(judged)}/{len(judged)}" if judged else "-"
        errs = sum(a["arith_errors"] for a in audits)
        opts = sum(a["n_options"] for a in audits)
        out.append(f"| {n} | {len(audits)} | {argmax_rate} | {errs}/{opts} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("-o", "--output")
    args = ap.parse_args(argv)
    md = summarise(args.run_dir)
    if args.output:
        try:
            atomic_write_text(ensure_inside(args.run_dir, args.output), md + "\n")
        except PathError as ex:
            print(f"ERROR {ex}", file=sys.stderr)
            return 2
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
