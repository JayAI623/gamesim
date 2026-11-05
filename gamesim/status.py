#!/usr/bin/env python3
"""Show the next step for each run under a base directory (multi-run orchestration helper).

Usage: status.py <base_dir_with_run_*> [--json]
Next steps: prep r / entities r (missing codes) / gm r / gm_commit r / roll r / gm_resolve r / apply r / done
A round counts as done only when turns/rNN/commit.json exists (pre-v0.8 run dirs: state.json round).
"""
from __future__ import annotations

import glob
import json
import os
import sys

try:
    from .turn import committed_rounds, load_run, move_path, terminal_round, turn_file
except ImportError:  # run as a script
    from turn import committed_rounds, load_run, move_path, terminal_round, turn_file


def next_step(run):
    data, _ = load_run(run)
    rounds = data["settings"]["rounds"]
    blind = data["settings"].get("blind_dice", False)
    committed = committed_rounds(run)
    c = len(committed)
    if terminal_round(run, committed):
        return {"step": "done", "round": c, "why": "terminal"}
    if c >= rounds:
        return {"step": "done", "round": c}
    r = c + 1
    if not turn_file(run, r, "dice.json").exists():
        return {"step": "prep", "round": r}
    missing = [e["codename"] for e in data["entities"] if not move_path(run, r, e["codename"]).exists()]
    if missing:
        return {"step": "entities", "round": r, "missing": missing}
    if blind:
        if not turn_file(run, r, "prompt_gm_commit.md").exists():
            return {"step": "gm", "round": r}
        if not turn_file(run, r, "gm_commit.json").exists():
            return {"step": "gm_commit", "round": r}
        if not turn_file(run, r, "outcomes.json").exists():
            return {"step": "roll", "round": r}
    elif not turn_file(run, r, "prompt_gm.md").exists():
        return {"step": "gm", "round": r}
    if not turn_file(run, r, "gm.json").exists():
        return {"step": "gm_resolve", "round": r}
    return {"step": "apply", "round": r}


def status(base) -> dict:
    return {os.path.basename(run): next_step(run) for run in sorted(glob.glob(os.path.join(base, "run_*")))}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = status(os.path.expanduser(argv[0]))
    if "--json" in argv:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for k, v in out.items():
            extra = f" missing={','.join(v['missing'])}" if v.get("missing") else ""
            print(f"{k}: r{v['round']} {v['step']}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
