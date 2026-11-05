#!/usr/bin/env python3
"""Aggregate the runs of one gamesim into k/n plus a markdown table.

Usage: aggregate.py <run_dir> [<run_dir> ...]   (or a base dir containing run_*)
         [--kind ever|at_deadline|sustained] [--min-rounds N] [--planned N] [--json] [-o out.md]

- hit per round comes from state history (`hit`, falling back to the pre-v0.8 alias `recovered`).
- A run is complete when it committed all rounds or stopped on a terminal round (absorbing: later rounds keep
  the last state). ever / sustained count the rounds actually played; at_deadline uses the last played round.
- n is fixed to the planned number of runs; if any run is incomplete, `complete` is false and no k should be
  registered.
- Missing variables / fields render as '-'.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

try:
    from . import target as tg
    from .fsio import atomic_write_text, ensure_inside, jload, safe_path
    from .turn import agreement_of, committed_rounds, is_legacy, load_run, rebuild, round_file
except ImportError:  # run as a script
    import target as tg
    from fsio import atomic_write_text, ensure_inside, jload, safe_path
    from turn import agreement_of, committed_rounds, is_legacy, load_run, rebuild, round_file


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _fmt(x):
    if x is None:
        return "-"
    return f"{x:g}" if _num(x) else str(x)


def run_history(run, data, variables, committed):
    if is_legacy(run):
        hist = jload(safe_path(run, "state.json")).get("history", [])
        return hist[:len(committed)]
    state, _ = rebuild(run, data, variables, len(committed))
    return state["history"]


def summarize(run) -> dict:
    data, variables = load_run(run)
    committed = committed_rounds(run)
    hist = run_history(run, data, variables, committed)
    target = data.get("target") or {}
    rounds = data["settings"]["rounds"]
    term = next((h["round"] for h in hist if tg.truthy(h.get("terminal"))), None)
    esc = []
    for k in committed:
        r = jload(round_file(run, k))
        esc += [m.get("chosen", {}).get("escalation") for m in r.get("moves", {}).values() if isinstance(m, dict)]
    esc = [e for e in esc if _num(e)]
    _, cnt = Counter(esc).most_common(1)[0] if esc else (None, 0)
    last = hist[-1] if hist else {}

    def p_of(h):
        return h.get("p_target", h.get("p_recovery_6m"))

    def ci_of(h):
        return h.get("p_target_ci90", h.get("p_recovery_ci90"))

    def ag_of(h):
        return h.get("agreement") or h.get("ceasefire") or {}

    tvars = [c.get("var") for c in target.get("conds") or []] or list(variables)[:2]
    models = data.get("settings", {}).get("models") or {}
    return {
        "run": os.path.basename(str(run).rstrip("/")),
        "kind": target.get("kind"), "min_rounds": target.get("min_rounds"),
        "rounds_planned": rounds, "rounds_played": len(hist), "terminal_round": term,
        "complete": bool(term) or len(hist) >= rounds,
        "hits": [tg.hist_hit(h) for h in hist],
        "agreement_name": (agreement_of(data) or {}).get("name"),
        "agreement_rounds": sum(1 for h in hist if tg.truthy(ag_of(h).get("in_effect"))),
        "target_vars": {v: (last.get("state_vars") or {}).get(v) for v in tvars},
        "p_target_last": p_of(last), "p_target_ci90_last": ci_of(last),
        "p_path": [p_of(h) for h in hist],
        "fixes": "on" if data.get("settings", {}).get("blind_dice") else "off",
        "models": "sonnet×%d" % len(models) if models else "default",
        "esc_off_modal": f"{len(esc) - cnt}/{len(esc)}", "esc_max": max(esc) if esc else None,
    }


def expand(args) -> list[str]:
    runs = []
    for a in args:
        a = os.path.expanduser(a)
        subs = sorted(glob.glob(os.path.join(a, "run_*")))
        runs += subs if subs else [a]
    return runs


def compute_k(run_dirs, kind=None, min_rounds=None, planned_n=None) -> dict:
    """k/n over the planned runs. `complete` is false if any planned run is missing or unfinished."""
    rows = [summarize(r) for r in run_dirs]
    kind = kind or next((r["kind"] for r in rows if r["kind"]), None) or "ever"
    if min_rounds is None:
        min_rounds = next((r["min_rounds"] for r in rows if r["min_rounds"]), None)
    n = planned_n if planned_n is not None else len(rows)
    incomplete = [r["run"] for r in rows if not r["complete"]]
    if len(rows) < n:
        incomplete.append(f"{n - len(rows)} planned run(s) missing")
    for r in rows:
        r["hit"] = tg.run_hit(r["hits"], kind, min_rounds) if r["complete"] else None
    k = sum(1 for r in rows if r["hit"])
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "kind": kind, "min_rounds": min_rounds, "complete": not incomplete and len(rows) == n,
            "incomplete": incomplete, "wilson95": [round(lo, 4), round(hi, 4)], "runs": rows}


def markdown(res: dict) -> str:
    rows = res["runs"]
    ag = next((r["agreement_name"] for r in rows if r["agreement_name"]), None) or "协议"
    tv = list(rows[0]["target_vars"]) if rows else []
    lines = [f"| 局 | 轮数 | 终止轮 | 命中轮 | 本局命中 | 末轮命中 | {ag}轮数 | 末轮 {'/'.join(tv) or '-'} | "
             "末轮 p_target(90%区间) | p_target 轨迹 | 非众数升级 | 最高升级 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ci = r["p_target_ci90_last"] if r["p_target_ci90_last"] else "-"
        path = "→".join(f"{p:.2f}" if _num(p) else "-" for p in r["p_path"]) or "-"
        hit_rounds = ",".join(str(i + 1) for i, h in enumerate(r["hits"]) if h) or "-"
        hit = "-" if r["hit"] is None else ("是" if r["hit"] else "否")
        final = "是" if r["hits"] and r["hits"][-1] else "否"
        tvals = "/".join(_fmt(r["target_vars"].get(v)) for v in tv) or "-"
        lines.append(f"| {r['run']} | {r['rounds_played']}/{r['rounds_planned']} | {_fmt(r['terminal_round'])} | "
                     f"{hit_rounds} | {hit} | {final} | {r['agreement_rounds']} | {tvals} | "
                     f"{_fmt(r['p_target_last'])} ({ci}) | {path} | {r['esc_off_modal']} | {_fmt(r['esc_max'])} |")
    lo, hi = res["wilson95"]
    kind = res["kind"] + (f"(min_rounds={res['min_rounds']})" if res["kind"] == "sustained" else "")
    ps = [r["p_target_last"] for r in rows if _num(r["p_target_last"])]
    tail = (f"；末轮 GM p_target 均值 {sum(ps) / len(ps):.2f}（范围 {min(ps):.2f}–{max(ps):.2f}）" if ps else "")
    status = "" if res["complete"] else f"；**未完成：{', '.join(res['incomplete'])}，不登记预测**"
    lines.append(f"\n{kind}：{res['k']}/{res['n']} 局命中（模拟命中频率的 Wilson 95% 区间 {lo:.0%}–{hi:.0%}）"
                 f"{tail}{status}")
    return "\n".join(lines)


def digest(run_dirs, max_chars: int = 160) -> str:
    """Compact per-round GM digest for prompts/result_summary.md."""
    out = []
    for run in run_dirs:
        data, variables = load_run(run)
        out.append(f"### {os.path.basename(str(run).rstrip('/'))}")
        for k in committed_rounds(run):
            gm = (jload(round_file(run, k)).get("gm") or {})
            p = tg.period_of(data, k)
            per = f"（{p['start']}–{p['end']}）" if p else ""
            sv = gm.get("state_vars") or {}
            ag = gm.get("agreement") or gm.get("ceasefire") or {}
            ptar = gm.get("p_target", gm.get("p_recovery_6m"))
            reason = gm.get("p_target_reason", gm.get("p_recovery_reason", ""))
            out.append(f"- R{k}{per}：{str(gm.get('last_round_public', ''))[:max_chars]}"
                       f" | 状态 " + " ".join(f"{s}={_fmt(sv.get(s))}" for s in variables)
                       + f" | 协议有效 {_fmt(ag.get('in_effect'))} | p_target {_fmt(ptar)}：{str(reason)[:80]}"
                       + (f" | 终止：{str(gm.get('terminal_reason', ''))[:80]}" if tg.truthy(gm.get("terminal")) else ""))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--kind", choices=tg.KINDS)
    ap.add_argument("--min-rounds", type=int)
    ap.add_argument("--planned", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    runs = expand(a.dirs)
    res = compute_k(runs, a.kind, a.min_rounds, a.planned)
    md = markdown(res)
    if a.output:
        root = Path(runs[0]).resolve().parent if runs else Path.cwd()
        atomic_write_text(ensure_inside(root, a.output), md + "\n")
    print(json.dumps(res, ensure_ascii=False) if a.json else md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
