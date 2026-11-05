"""Fork a run for a stress test: copy src up to round N, inject events at round N+1, play on.

  gamesim fork <base>/run_3 <base>/run_4 --upto 2 --events stress.json

- dst gets src's committed rounds 1..N (turns/, rounds/), everything later is deleted, state.json and private/*
  are rebuilt from the kept rounds (cmd_rebuild). Same seed as src unless --reseed: later dice are identical, so
  the only difference from src is the injected events (and the models' own randomness).
- If src swapped branches after a round >= N+1 (the swap has not happened yet at the fork point), the pre-swap
  entities.json is restored; pass --swap to swap again in the fork.
- Events at round N+1 = the user events src had at that round (e.g. the branch announcement) + yours, unless
  --drop-src-events. dst must sit next to src (same base dir), as the engine only reads events from there.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import runner, turn
from .fsio import jdump, jload


def fork(src, dst, upto: int, events_file=None, *, keep_src_events=True, reseed=None) -> dict:
    src, dst = Path(src), Path(dst)
    if not (src / "run.json").exists():
        raise FileNotFoundError(f"{src} is not a run dir (no run.json)")
    have = turn.committed_rounds(str(src))
    if upto < 0 or upto > len(have):
        raise ValueError(f"--upto {upto}: src has committed rounds {have or 'none'}")
    if dst.exists():
        raise FileExistsError(f"{dst} exists; pick a new name")
    if src.resolve().parent != dst.resolve().parent:
        raise ValueError("dst must be in the same base dir as src")
    at = upto + 1
    src_events = []
    uf = src / "turns" / f"r{at:02d}" / "user_events.json"
    if keep_src_events and uf.exists():
        src_events = jload(uf).get("events", [])
    shutil.copytree(src, dst)
    for d in dst.glob("turns/r*"):
        if int(d.name[1:]) > upto:
            shutil.rmtree(d)
    for f in dst.glob("rounds/round_*.json"):
        if int(f.stem.split("_")[1]) > upto:
            f.unlink()
    for f in dst.glob("events_r*.json"):
        if int(f.stem.split("_")[1][1:3]) > upto:
            f.unlink()
    bf = dst / "branch.json"
    if bf.exists() and int(jload(bf).get("after_round", 0)) >= at and (dst / "entities.pre_swap.json").exists():
        shutil.copy(dst / "entities.pre_swap.json", dst / "entities.json")
        bf.unlink()
        src_events = []
    if reseed is not None:
        meta = jload(dst / "run.json")
        meta["seed"] = int(reseed)
        jdump(meta, dst / "run.json")
    turn.cmd_rebuild(str(dst))
    assert turn.committed_rounds(str(dst)) == list(range(1, upto + 1))
    evs = list(src_events)
    if events_file:
        raw = jload(events_file)
        evs += raw.get("events") if isinstance(raw, dict) else raw
    ef = None
    if evs:
        for i, ev in enumerate(evs, 1):
            ev["seq"] = i
        ef = dst / f"events_r{at:02d}_fork.json"
        jdump(evs, ef)
    jdump({"src": src.name, "upto": upto, "events": [ev["id"] for ev in evs], "reseed": reseed},
          dst / "fork.json")
    return {"run": str(dst), "at": at, "events_file": str(ef) if ef else None, "events": [e["id"] for e in evs]}


def fork_and_play(src, dst, upto, events_file=None, *, keep_src_events=True, reseed=None, swap_file=None, **kw):
    info = fork(src, dst, upto, events_file, keep_src_events=keep_src_events, reseed=reseed)
    swap = runner.load_swap(swap_file) if swap_file else None
    ev = {info["at"]: info["events_file"]} if info["events_file"] else {}
    stats = runner.play([info["run"]], swap=swap, events_at=ev, first_round=info["at"], **kw)
    return {**info, "stats": stats, "fallback": runner.fallback_stats([info["run"]])}
