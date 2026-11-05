"""gamesim command line.

  gamesim check     <entities.json>                          schema + pitfall lint (red lines at round 0, …)
  gamesim bdm       <entities.json> -o out.json [--md out.md] BDM expected-utility baseline
  gamesim run       <base> --entities E [--runs 3] [--swap S]  layer 1: N runs, every role/GM via claude -p
  gamesim fork      <src_run> <dst_run> --upto N [--events F] stress test: copy to round N, inject, play on
  gamesim status    <base>                                    next step per run + fallback / raw-output counts
  gamesim aggregate <base or run dirs…>                       target hit k/n with Wilson interval
  gamesim score     <strategy.json> <base> [--runs a,b]       layer 2: strategy arms per decision maker
  gamesim sweep     <strategy.json> <base> --run R --from-round K --set S=50,80,95   find ranking flips

--fake uses the bundled fake claude (no model calls) — for trying the pipeline end to end.
Engine scripts stay available as modules: python -m gamesim.turn | ingest | utility | compare | dice.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import aggregate, bdm, runner, schema, status
from . import target as tg
from .fsio import jload
from .turn import delta_of
from .utility import check_weights, utility


def months_per_round(data) -> float:
    """From periods[0] if present, else from the head of settings.time_per_round ("1年（…）" -> 12)."""
    per = data.get("periods") or []
    if per:
        return tg.months_between(per[0]["start"], per[0]["end"])
    head = str(data.get("settings", {}).get("time_per_round", "")).split("（")[0].split("(")[0]
    return 12.0 if ("年" in head or "year" in head.lower()) else 1.0


def lint(data) -> tuple[list[str], list[str]]:
    """(errors, warnings) for an entities.json: the schema plus the pitfalls in docs/METHOD.md."""
    errs = schema.validate_entities(data, require_target=False, require_periods=False)
    warn = []
    if errs:
        return errs, warn
    variables = {v["sym"]: v for v in data["variables"]}
    st0 = {s: v["current"] for s, v in variables.items()}
    st = data.get("settings", {})
    yearly = months_per_round(data) >= 6
    for e in data["entities"]:
        n = e["name"]
        try:
            check_weights(e)
        except ValueError as ex:
            errs.append(str(ex))
        u = utility(e, variables, st0)
        if u["red_lines_breached"]:
            errs.append(f"{n}：第 0 轮就突破红线 {u['red_lines_breached']}（U=−10）。红线只放灾难点，"
                        "「到阈值就做某事」的行为规则放 soft_rules（坑①）")
        for t in e.get("utility", []):
            if t.get("direction") == "-" and t.get("shape") == "convex":
                warn.append(f"{n}.{t['var']}：'-' + convex 表示越接近上限边际损失越小；"
                            "想表达「越接近阈值损失越大」要用 concave（坑②）")
        if st.get("discount") and yearly and "delta" not in e:
            warn.append(f"{n}：discount 开着、每轮按年，但没写 delta；默认月因子 {delta_of(e)}，"
                        f"一年后只剩 {delta_of(e) ** 12:.2f}（坑③）")
        hr = str(e.get("oos_hit_rate", ""))
        if "/" in hr:
            try:
                a, b = (float(x) for x in hr.split("/")[:2])
                if b and a / b < 0.5:
                    warn.append(f"{n}：样本外命中 {hr}，对齐页要标为「拿不准」（坑⑧）")
            except ValueError:
                pass
    return errs, warn


def _fake(a):
    if getattr(a, "fake", False):
        os.environ["GAMESIM_CLAUDE_BIN"] = runner.fake_claude_cmd()


def cmd_check(a) -> int:
    errs, warn = lint(jload(a.entities))
    for w in warn:
        print("WARN  " + w)
    for e in errs:
        print("ERROR " + e)
    print(f"{len(errs)} error(s), {len(warn)} warning(s)")
    return 2 if errs else 0


def cmd_run(a) -> int:
    _fake(a)
    runner.setup_logging(a.base)
    res = runner.run_all(a.base, a.entities, a.runs, swap_file=a.swap, seed_base=a.seed_base, model=a.model,
                         gm_model=a.gm_model, parallel=a.parallel, tries=a.tries)
    print(res["markdown"])
    print("fallback:", json.dumps({k: v["fallback"] for k, v in res["fallback"].items()}, ensure_ascii=False))
    return 0


def cmd_fork(a) -> int:
    _fake(a)
    from . import fork
    runner.setup_logging(os.path.dirname(os.path.abspath(a.dst)))
    res = fork.fork_and_play(a.src, a.dst, a.upto, a.events, keep_src_events=not a.drop_src_events,
                             reseed=a.reseed, swap_file=a.swap, model=a.model, gm_model=a.gm_model,
                             parallel=a.parallel, tries=a.tries)
    print(json.dumps({k: v for k, v in res.items() if k != "stats"}, ensure_ascii=False, indent=1))
    return 0


def cmd_status(a) -> int:
    st = status.status(a.base)
    fb = runner.fallback_stats(a.base)
    for k, v in st.items():
        extra = f" missing={','.join(v['missing'])}" if v.get("missing") else ""
        f = fb.get(k, {})
        print(f"{k}: r{v['round']} {v['step']}{extra} | fallback {f.get('fallback', 0)}/{f.get('moves', 0)} "
              f"{f.get('by_entity') or ''} | raw {f.get('raw_failures', 0)}")
    return 0


def cmd_score(a) -> int:
    _fake(a)
    from . import strategy
    s = strategy.score_all(a.config, a.base, a.runs.split(",") if a.runs else None, a.out)
    print(s["markdown"])
    return 0


def cmd_sweep(a) -> int:
    from . import strategy
    grid = {}
    for item in a.set:
        k, vs = item.split("=", 1)
        grid[k.strip()] = [float(x) for x in vs.split(",")]
    res = strategy.sweep(a.config, a.base, a.run, a.from_round, grid, a.dm.split(",") if a.dm else None, a.out)
    for tag, v in res.items():
        print(f"{tag}: " + " > ".join(f"{x}({v['totals'][x]:.2f})" for x in v["ranking"])
              + ("   <- 第一名翻转" if v["flip"] else ""))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="gamesim", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def llm(p):
        p.add_argument("--model", default=runner.DEFAULT_MODEL)
        p.add_argument("--gm-model")
        p.add_argument("--parallel", type=int, default=runner.PARALLEL)
        p.add_argument("--tries", type=int, default=runner.TRIES)
        p.add_argument("--fake", action="store_true", help="use the bundled fake claude")

    p = sub.add_parser("check")
    p.add_argument("entities")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("bdm")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=lambda a: bdm.main(a.rest))
    p = sub.add_parser("aggregate")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=lambda a: aggregate.main(a.rest))
    p = sub.add_parser("status")
    p.add_argument("base")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("run")
    p.add_argument("base")
    p.add_argument("--entities")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--swap")
    p.add_argument("--seed-base", type=int)
    llm(p)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("fork")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--upto", type=int, required=True)
    p.add_argument("--events")
    p.add_argument("--drop-src-events", action="store_true")
    p.add_argument("--reseed", type=int)
    p.add_argument("--swap")
    llm(p)
    p.set_defaults(fn=cmd_fork)
    p = sub.add_parser("score")
    p.add_argument("config")
    p.add_argument("base")
    p.add_argument("--runs", help="comma-separated run dir names (default: all run_*)")
    p.add_argument("--out", help="output dir (default <base>/layer2)")
    p.add_argument("--fake", action="store_true")
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("sweep")
    p.add_argument("config")
    p.add_argument("base")
    p.add_argument("--run", required=True)
    p.add_argument("--from-round", type=int, required=True)
    p.add_argument("--set", action="append", required=True, help="VAR=v1,v2,…  (repeatable)")
    p.add_argument("--dm")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_sweep)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as ex:
        print(f"ERROR {type(ex).__name__}: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
