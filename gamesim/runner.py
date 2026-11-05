"""Multi-run driver: N runs of one entities.json, every role and the GM played by `claude -p`.

  base/
    entities.json        the scenario (copied in by `gamesim run`)
    swap.json            optional branch swap (copied in), see apply_swap
    run_1/ … run_N/      one engine run dir each (seed = settings.seed + k - 1, or --seed-base + k)
    k.md                 aggregate table (target hit k/n, Wilson interval)
    runner.log

Per round: prep every unfinished run, play all missing roles in parallel (validation failure -> retry with the
error appended; every failed raw output is kept as turns/rNN/raw_<code>_<i>.txt; after the retries the role is
recorded as the fallback move 维持现状/观望), then drive each run's GM steps (legacy or blind dice) to commit.
Everything is resumable: re-running skips files that already exist.

Claude is called as `claude -p` with no tools, no settings, no MCP servers and no session persistence; the prompt
goes on stdin and the cwd is an empty temp dir. GAMESIM_CLAUDE_BIN overrides the binary (a path, or a command line
such as "python3 /path/to/fake_claude.py"). No API key is used anywhere.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import random
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import status as status_mod
from . import turn
from .aggregate import compute_k, markdown
from .conditions import apply_patch
from .fsio import jdump, jload
from .ingest import ingest_round, write_fallback_move

log = logging.getLogger("gamesim.runner")

DEFAULT_MODEL = "sonnet"
ROLE_TIMEOUT, GM_TIMEOUT = 400, 700
TRIES = 3          # retries after the first attempt (roles, GM and scorer alike)
PARALLEL = 15
RETRY_TAIL = "\n\n上一次的输出没通过校验：{err}\n改正后重新只输出一个 JSON 代码块。"
FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


# ---------- claude ----------
def claude_bin() -> list[str]:
    """GAMESIM_CLAUDE_BIN (default `claude`) as an argv prefix. An existing path is taken whole (spaces allowed);
    anything else is split like a shell command line."""
    b = os.environ.get("GAMESIM_CLAUDE_BIN", "claude")
    return [b] if os.path.exists(b) else shlex.split(b)


def claude_argv(model: str) -> list[str]:
    return [*claude_bin(), "-p", "--output-format", "text", "--no-session-persistence", "--setting-sources", "",
            "--strict-mcp-config", "--disable-slash-commands", "--tools", "", "--model", model]


def fake_claude_cmd() -> str:
    """Command line for the bundled fake claude (tests, dry runs)."""
    return shlex.join([sys.executable, str(FAKE_CLAUDE)])


def call_claude(prompt: str, model: str = DEFAULT_MODEL, timeout: int = ROLE_TIMEOUT) -> str:
    with tempfile.TemporaryDirectory() as cwd:
        p = subprocess.run(claude_argv(model), input=prompt, capture_output=True, text=True, cwd=cwd,
                           timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"claude rc {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    return p.stdout


# ---------- one role / one GM ----------
def _rdir(run, r) -> Path:
    return Path(run) / "turns" / f"r{r:02d}"


def play_role(run, r, code, model=DEFAULT_MODEL, timeout=ROLE_TIMEOUT, tries=TRIES, call=None):
    """One role's move for round r. Returns "ok" or "fallback"."""
    call = call or call_claude
    prompt = (_rdir(run, r) / f"prompt_{code}.md").read_text(encoding="utf-8")
    err = None
    for i in range(tries + 1):
        text = prompt if err is None else prompt + RETRY_TAIL.format(err=err)
        out = ""
        try:
            out = call(text, model, timeout)
            ingest_round(str(run), r, "move", out, code)
            return "ok"
        except Exception as e:  # noqa: BLE001 - every failure is retried, then recorded
            err = str(e)[:300]
            (_rdir(run, r) / f"raw_{code}_{i}.txt").write_text(f"ERR: {err}\n\n{out}", encoding="utf-8")
    log.warning("fallback %s r%d %s: %s", run, r, code, err)
    write_fallback_move(str(run), r, code, (err or "")[:200])
    return "fallback"


def drive_gm(run, r, model=DEFAULT_MODEL, timeout=GM_TIMEOUT, tries=TRIES, call=None):
    """Drive run's GM steps for round r (gm -> [gm_commit -> roll] -> gm_resolve -> apply) until committed."""
    call = call or call_claude
    run = str(run)
    for _ in range(12):
        st = status_mod.next_step(run)
        if st["step"] == "done" or st["round"] != r:
            return
        step = st["step"]
        if step == "gm":
            turn.cmd_gm(run, r, "stdout")
        elif step == "roll":
            turn.cmd_roll(run, r, "stdout")
        elif step == "apply":
            res = turn.cmd_apply(run, r, 1)
            for line in res.get("summary") or []:
                log.info("%s %s", Path(run).name, line)
        elif step in ("gm_commit", "gm_resolve"):
            name, kind = ("prompt_gm_commit.md", "gm_commit") if step == "gm_commit" else ("prompt_gm.md", "gm")
            prompt = (_rdir(run, r) / name).read_text(encoding="utf-8")
            err = None
            for i in range(tries + 1):
                text = prompt if err is None else prompt + RETRY_TAIL.format(err=err)
                out = ""
                try:
                    out = call(text, model, timeout)
                    ingest_round(run, r, kind, out)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:300]
                    (_rdir(run, r) / f"raw_{kind}_{i}.txt").write_text(f"ERR: {err}\n\n{out}", encoding="utf-8")
            if err is not None:
                raise RuntimeError(f"{run} r{r} {kind} failed after {tries + 1} attempts: {err}")
        else:
            raise RuntimeError(f"{run} r{r} stuck at {step}")
    raise RuntimeError(f"{run} r{r} no progress")


def par(fn, args, n):
    if not args:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(n, len(args)))) as pool:
        return [f.result() for f in [pool.submit(fn, *a) for a in args]]


# ---------- branch swap ----------
def load_swap(path) -> dict | None:
    if not path:
        return None
    sw = jload(path)
    for k in ("after_round", "branches"):
        if k not in sw:
            raise ValueError(f"swap file needs '{k}'")
    for side, br in sw["branches"].items():
        if not isinstance(br.get("patch", []), list):
            raise ValueError(f"swap branch {side}: patch must be a list of JSON-Patch ops")
    return sw


def branch_of(sw: dict, k: int, seed: int) -> tuple[str, float | None]:
    """Stratified: swap['assign'][str(k)]; else a seeded draw from swap['p'] (or uniform over branches)."""
    side = (sw.get("assign") or {}).get(str(k))
    if side is not None:
        if side not in sw["branches"]:
            raise ValueError(f"assign[{k}] = {side!r} is not a branch")
        return side, None
    p = sw.get("p") or {s: 1 / len(sw["branches"]) for s in sw["branches"]}
    x = random.Random(seed * 1009 + k).random()
    acc = 0.0
    for s, ps in p.items():
        acc += ps
        if x < acc:
            return s, round(x, 4)
    return list(p)[-1], round(x, 4)


def branch_events(br: dict, side: str) -> list[dict]:
    evs = br.get("events") or ([br["event"]] if br.get("event") else [])
    out = []
    for i, ev in enumerate(evs, 1):
        out.append(ev if isinstance(ev, dict) else {"id": f"branch_{side}_{i}", "seq": i, "text": ev})
    return out


def apply_swap(run, sw: dict, k: int) -> str | None:
    """Before round after_round+1: patch run/entities.json once (entities.pre_swap.json keeps the original),
    write run/events_rNN.json with the branch's public event(s); return that events file (or None)."""
    run = Path(run)
    r = int(sw["after_round"]) + 1
    bf = run / "branch.json"
    if not bf.exists():
        meta = turn.run_meta(str(run)) or {}
        side, roll = branch_of(sw, k, int(meta.get("seed") or k))
        data = jload(run / "entities.json")
        jdump(data, run / "entities.pre_swap.json")
        jdump(apply_patch(data, sw["branches"][side].get("patch", [])), run / "entities.json")
        jdump({"side": side, "roll": roll, "after_round": sw["after_round"]}, bf)
    side = jload(bf)["side"]
    evs = branch_events(sw["branches"][side], side)
    if not evs:
        return None
    ef = run / f"events_r{r:02d}.json"
    jdump(evs, ef)
    return str(ef)


def layer1_runs(base) -> list[str]:
    """run_* dirs that are not forks (fork.json marks a stress fork; score those explicitly)."""
    return sorted(p.name for p in Path(base).glob("run_*")
                  if (p / "run.json").exists() and not (p / "fork.json").exists())


def run_weights(base, runs=None) -> dict[str, float]:
    """Weight per run for aggregation. With a swap that has p: p[side] / (#runs on that side); else equal."""
    base = Path(base)
    runs = runs or layer1_runs(base)
    sw = jload(base / "swap.json") if (base / "swap.json").exists() else None
    sides = {}
    for name in runs:
        bf = base / name / "branch.json"
        sides[name] = jload(bf)["side"] if bf.exists() else None
    if sw and sw.get("p") and all(sides.values()):
        cnt = {}
        for s in sides.values():
            cnt[s] = cnt.get(s, 0) + 1
        w = {n: sw["p"].get(s, 0) / cnt[s] for n, s in sides.items()}
        tot = sum(w.values()) or 1
        return {n: v / tot for n, v in w.items()}
    return {n: 1 / len(runs) for n in runs} if runs else {}


# ---------- the loop ----------
def setup_runs(base, n, seed_base=None) -> list[Path]:
    base = Path(base)
    data = jload(base / "entities.json")
    runs = []
    for k in range(1, n + 1):
        run = base / f"run_{k}"
        if not (run / "run.json").exists():
            run.mkdir(parents=True, exist_ok=True)
            d = copy.deepcopy(data)
            s0 = seed_base if seed_base is not None else int(d["settings"].get("seed", 0)) - 1
            d["settings"]["seed"] = s0 + k
            jdump(d, run / "entities.json")
            turn.cmd_init(str(run))
        runs.append(run)
    return runs


def play(runs, *, model=DEFAULT_MODEL, gm_model=None, swap=None, events_at=None, parallel=PARALLEL,
         role_timeout=ROLE_TIMEOUT, gm_timeout=GM_TIMEOUT, tries=TRIES, call=None, first_round=1):
    """Play every run to its end. events_at = {round: events_file} injects user events (same file for all runs).
    swap is applied to run_k (k from the dir name) right before round after_round+1 is prepped."""
    runs = [Path(x) for x in runs]
    events_at = events_at or {}
    data, _ = turn.load_run(str(runs[0]))
    stats = {"fallback": 0, "ok": 0}
    for r in range(first_round, data["settings"]["rounds"] + 1):
        t0 = time.time()
        active = [x for x in runs if status_mod.next_step(str(x))["step"] != "done"
                  and r not in turn.committed_rounds(str(x))]
        if not active:
            continue
        jobs = []
        for run in active:
            if not (_rdir(run, r) / "dice.json").exists():
                files = []
                if swap and r == int(swap["after_round"]) + 1:
                    tail = run.name.split("_")[-1]
                    files.append(apply_swap(run, swap, int(tail) if tail.isdigit() else 0))
                files.append(events_at.get(r))
                files = [f for f in files if f]
                turn.cmd_prep(str(run), r, "stdout", merge_events(run, r, files) if files else None)
            rd, _ = turn.load_run(str(run))
            models = rd.get("settings", {}).get("models") or {}
            for e in rd["entities"]:
                if not (_rdir(run, r) / f"move_{e['codename']}.json").exists():
                    jobs.append((run, r, e["codename"], models.get(e["name"]) or model, role_timeout, tries, call))
        for res in par(play_role, jobs, parallel):
            stats[res] += 1
        par(drive_gm, [(run, r, gm_model or model, gm_timeout, tries, call) for run in active], parallel)
        log.info("ROUND %d done: %d runs, %d roles, %.0fs", r, len(active), len(jobs), time.time() - t0)
    return stats


def merge_events(run, r, files) -> str:
    """Concatenate event files (lists or {"events": [...]}) into run/events_rNN_in.json (inside the run dir, where
    the engine accepts it), renumbering seq."""
    evs = []
    for f in files:
        if not f:
            continue
        raw = jload(f)
        evs += raw.get("events") if isinstance(raw, dict) else raw
    for i, ev in enumerate(evs, 1):
        ev["seq"] = i
    out = Path(run) / f"events_r{r:02d}_in.json"
    jdump(evs, out)
    return str(out)


def fallback_stats(base_or_runs) -> dict:
    """Per run: fallback moves (by entity) and raw failed outputs kept. Refusals end up here (see METHOD.md)."""
    if isinstance(base_or_runs, (str, Path)):
        runs = sorted(p for p in Path(base_or_runs).glob("run_*") if p.is_dir())
    else:
        runs = [Path(x) for x in base_or_runs]
    out = {}
    for run in runs:
        fb, raw = {}, 0
        for mv in run.glob("turns/r*/move_*.json"):
            try:
                m = jload(mv)
            except Exception:  # noqa: BLE001
                continue
            if m.get("fallback"):
                name = m.get("entity", mv.stem[5:])
                fb[name] = fb.get(name, 0) + 1
        raw = len(list(run.glob("turns/r*/raw_*.txt")))
        moves = len(list(run.glob("turns/r*/move_*.json")))
        out[run.name] = {"moves": moves, "fallback": sum(fb.values()), "by_entity": fb, "raw_failures": raw}
    return out


def run_all(base, entities=None, n=3, *, swap_file=None, seed_base=None, planned_n=None, **kw) -> dict:
    """`gamesim run`: copy inputs into base, set up n runs, play them, write k.md."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    if entities:
        src = jload(entities)
        if (base / "entities.json").exists() and jload(base / "entities.json") != src:
            raise ValueError(f"{base}/entities.json differs from {entities}; use a new base dir")
        jdump(src, base / "entities.json")
    if not (base / "entities.json").exists():
        raise FileNotFoundError(f"{base}/entities.json missing (pass --entities)")
    swap = load_swap(swap_file)
    if swap:
        jdump(swap, base / "swap.json")
    elif (base / "swap.json").exists():
        swap = load_swap(base / "swap.json")
    runs = setup_runs(base, n, seed_base)
    stats = play(runs, swap=swap, **kw)
    res = compute_k([str(x) for x in runs], planned_n=planned_n or n)
    md = markdown(res)
    (base / "k.md").write_text(md + "\n", encoding="utf-8")
    fs = fallback_stats(runs)
    jdump(fs, base / "fallback_stats.json")
    return {"k": res, "markdown": md, "stats": stats, "fallback": fs}


def setup_logging(base):
    Path(base).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(Path(base) / "runner.log", encoding="utf-8"),
                                  logging.StreamHandler()], force=True)
