"""Layer 2: score decision makers' strategy arms on every layer-1 trajectory.

Layer 1 (runner) produces trajectories of shared state variables. Layer 2 asks: for a decision maker who cannot
move those variables (a household, a firm, an investor), which fixed strategy arm does best along them?

For each (run, decision maker) one scorer call (`claude -p`) reads the trajectory and the scoring rules and gives
the F components (0–100) for every arm × round. Then code — not the model — computes per round

    F   = min(cap, Σ_c weight_c · component_c)
    U   = Σ_terms weight · exposure[var][arm] · u(var)    (u from gamesim.utility.term_value; var "F" ∈ [0, 100])
    U   = −10 if a red line of the decision maker (for that arm) is breached that round
    tot = Σ_k discount_per_round^(k−1) · U_k

and ranks arms by the run-weighted total, with sensitivity scenarios (±30 % exposure, ±30 % on a component …),
a decomposition (mean F, components, non-F part of U) and a sweep that overrides state variables from some round on
to find where the ranking flips. Everything is driven by one config JSON (see examples/diaspora/strategy.json).
"""
from __future__ import annotations

import itertools
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import runner, turn
from . import target as tg
from .fsio import extract_json, jdump, jload
from .utility import OPS, RED_LINE_PENALTY, term_value

F_VAR = "F"
F_DEF = {"lo": 0, "hi": 100}
SHAPES = ("linear", "concave", "convex", "threshold", "peak")
DEFAULT_SENS = [{"name": "exposure-30%", "exposure_scale": 0.7}, {"name": "exposure+30%", "exposure_scale": 1.3}]

PROMPT = """你是{role}打分员。不要调用任何工具。下面是一条局势轨迹（第一层博弈推演产出，共 {n} 轮，每轮 {tpr}），以及打分规则。
{notes}
请为决策者 **{dm_name}** 的 {n_arms} 个策略臂逐轮给 F 的 {n_comp} 个分项打分（0–100），严格按规则里的口径，并写出依据（≤25 字）。
每个策略臂从第 1 轮执行到最后一轮，中途不换臂。

决策者：`{dm_id}`
策略臂字段：{arm_ids}
分项字段：{comp_ids}
轮次标签：{labels}

## 打分规则
{rules}

## F 的分项
{comp_table}

## 决策者设定
{profile}

## 策略臂
{arm_table}

## 分支：{side}

## 轨迹（state 是 GM 裁决后的状态变量；impact 是 GM 单列的影响栏，public 是公开局势摘要）
{traj}

## 输出
只输出一个 JSON 代码块：
```json
{{"dm": "{dm_id}", "arms": {{{arm_example}}},
 "notes": "≤80字：这条轨迹上对该决策者最关键的 1–2 个事件"}}
```
其中每个臂是长度为 {n} 的数组，按轮次顺序，每项形如 {row_example}。"""


# ---------- config ----------
def load_config(path) -> dict:
    path = Path(path)
    return load_config_dict(json.loads(path.read_text(encoding="utf-8")), path.parent)


def load_config_dict(cfg: dict, base_dir=None) -> dict:
    """Fill defaults; scorer_rules_file is read relative to base_dir (the config's folder)."""
    if cfg.get("scorer_rules_file"):
        cfg["scorer_rules"] = (Path(base_dir or ".") / cfg["scorer_rules_file"]).read_text(encoding="utf-8")
    cfg.setdefault("scorer_rules", "")
    cfg.setdefault("sensitivity", DEFAULT_SENS)
    cfg.setdefault("model", runner.DEFAULT_MODEL)
    cfg.setdefault("role", "策略")
    cfg.setdefault("F", {})
    cfg["F"].setdefault("cap", 100)
    return cfg


def arm_ids(cfg):
    return [a["id"] for a in cfg["arms"]]


def comp_keys(cfg):
    return [c["key"] for c in cfg["F"]["components"]]


def exposure(cfg, dm, var, ai, scale=1.0) -> float:
    ex = (cfg.get("exposure") or {}).get(var)
    if ex is None:
        e = 1.0
    elif isinstance(ex, list):
        e = float(ex[ai])
    else:
        e = float(ex.get(cfg["arms"][ai]["id"], 1.0))
    e *= float((dm.get("exposure_mult") or {}).get(var, 1.0))
    if e != 1:
        e = min(1.0, e * scale)
    return e


def validate(cfg, variables: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings). variables = {sym: var} of the layer-1 scenario."""
    err, warn = [], []
    arms = cfg.get("arms") or []
    if not arms:
        err.append("arms 不能为空")
    comps = (cfg.get("F") or {}).get("components") or []
    if not comps:
        err.append("F.components 不能为空")
    sw = sum(c.get("weight", 0) for c in comps)
    if comps and abs(sw - 1) > 0.02:
        warn.append(f"F 分项权重之和 {sw:.2f} ≠ 1")
    for var, ex in (cfg.get("exposure") or {}).items():
        if var not in variables:
            err.append(f"exposure.{var}：第一层没有这个变量")
        if isinstance(ex, list) and len(ex) != len(arms):
            err.append(f"exposure.{var} 的长度 {len(ex)} ≠ 策略臂数 {len(arms)}")
    ids = {a["id"] for a in arms}
    for dm in cfg.get("decision_makers") or []:
        who = dm.get("id", "?")
        tot = sum(t["weight"] for t in dm.get("utility", []))
        if abs(tot - 1) > 0.02:
            err.append(f"{who}：效用权重之和 {tot:.3f} ≠ 1")
        for t in dm.get("utility", []):
            if t["var"] != F_VAR and t["var"] not in variables:
                err.append(f"{who}：效用项 {t['var']} 不是第一层变量，也不是 F")
            if t.get("shape", "linear") not in SHAPES:
                err.append(f"{who}.{t['var']}：shape {t.get('shape')!r} 不在 {SHAPES}")
            if t.get("shape") in ("threshold", "peak") and t.get("threshold") is None:
                err.append(f"{who}.{t['var']}：{t['shape']} 需要 threshold")
            if t.get("direction") == "-" and t.get("shape") == "convex":
                warn.append(f"{who}.{t['var']}：direction '-' + convex = 越接近上限边际损失越小；"
                            "若想表达「越接近阈值损失越大」，用 concave（见 METHOD.md 坑②）")
        for rl in dm.get("red_lines", []):
            if rl.get("op") not in OPS:
                err.append(f"{who} 红线 {rl.get('rule')}：op 必须是 {list(OPS)}")
            bad = set(rl.get("arms") or []) - ids
            if bad:
                err.append(f"{who} 红线 {rl.get('rule')}：未知策略臂 {sorted(bad)}")
        for vm in dm.get("var_mods", []):
            if vm.get("var") not in variables:
                err.append(f"{who} var_mods：未知变量 {vm.get('var')}")
        if "discount_per_round" not in dm:
            warn.append(f"{who}：没写 discount_per_round，按 1（不折现）算；年度轮次要写年化因子（坑③）")
    return err, warn


# ---------- trajectories ----------
def trajectory(run, cfg) -> tuple[list[dict], str | None]:
    run = Path(run)
    data, variables = turn.load_run(str(run))
    rows = []
    marker = cfg.get("impact_marker")
    for k in turn.committed_rounds(str(run)):
        rec = jload(run / "rounds" / f"round_{k:02d}.json")
        gm = rec.get("gm") or {}
        ps = str(gm.get("public_state", ""))
        p = tg.period_of(data, k)
        row = {"round": k, "label": f"{p['start']}~{p['end']}" if p else f"R{k}",
               "state": {s: gm.get("state_vars", {}).get(s) for s in variables}}
        if marker:
            i = ps.find(marker)
            row["impact"] = ps[i:] if i >= 0 else "（GM 未单列）"
        row["public"] = ps[:int(cfg.get("public_chars", 600))]
        rows.append(row)
    bf = run / "branch.json"
    return rows, (jload(bf)["side"] if bf.exists() else None)


def render_profile(p) -> str:
    if isinstance(p, dict):
        return "\n".join(f"- {k}：{v}" for k, v in p.items())
    return str(p or "（无）")


def build_prompt(cfg, dm, rows, side, tpr="1 轮") -> str:
    arms, comps = cfg["arms"], cfg["F"]["components"]
    labels = [r["label"] for r in rows]
    row_ex = {"round": 1, "label": labels[0] if labels else "R1", **{c["key"]: 0 for c in comps}, "why": "…"}
    side_txt = (cfg.get("branch_labels") or {}).get(side, side) if side else "无分支"
    return PROMPT.format(
        role=cfg["role"], n=len(rows), tpr=tpr, dm_name=dm.get("name", dm["id"]), dm_id=dm["id"],
        notes="\n".join(cfg.get("scorer_notes") or []), n_arms=len(arms), n_comp=len(comps),
        arm_ids="、".join(f"`{a['id']}`" for a in arms), comp_ids="、".join(f"`{c['key']}`" for c in comps),
        labels="、".join(f"`{x}`" for x in labels), rules=cfg.get("scorer_rules") or "（见分项口径）",
        comp_table="\n".join(f"- `{c['key']}` {c.get('name', '')}（权重 {c['weight']}）：{c.get('rule', '')}"
                             for c in comps),
        profile=render_profile(dm.get("profile")),
        arm_table="\n".join(f"- `{a['id']}` {a.get('name', '')}：{a.get('desc', '')}" for a in arms),
        side=side_txt, traj=json.dumps(rows, ensure_ascii=False, indent=0),
        arm_example=", ".join(f'"{a["id"]}": [ … ]' for a in arms),
        row_example=json.dumps(row_ex, ensure_ascii=False))


def check_scores(obj, cfg, n) -> list[str]:
    if not isinstance(obj, dict) or not isinstance(obj.get("arms"), dict):
        return ["顶层要有 arms 对象"]
    err = []
    for a in arm_ids(cfg):
        rows = obj["arms"].get(a)
        if not isinstance(rows, list) or len(rows) != n:
            err.append(f"策略臂 {a} 要有 {n} 轮")
            continue
        for k, row in enumerate(rows, 1):
            for c in comp_keys(cfg):
                v = row.get(c) if isinstance(row, dict) else None
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 100:
                    err.append(f"{a} 第 {k} 轮 {c} 必须是 0–100 的数字")
    return err[:8]


def score_one(run, dm, cfg, out_dir, call=None, tries=runner.TRIES) -> dict:
    run, out_dir = Path(run), Path(out_dir)
    f = out_dir / f"{run.name}_{dm['id']}.json"
    if f.exists():
        return jload(f)
    rows, side = trajectory(run, cfg)
    data, _ = turn.load_run(str(run))
    prompt = build_prompt(cfg, dm, rows, side, data["settings"].get("time_per_round", "1 轮"))
    call = call or runner.call_claude
    err = None
    for i in range(tries + 1):
        text = prompt if err is None else prompt + f"\n\n上次输出不合格：{err}。只输出 JSON 代码块。"
        out = ""
        try:
            out = call(text, cfg["model"], int(cfg.get("timeout", 600)))
            obj = extract_json(out)
            errs = check_scores(obj, cfg, len(rows))
            if errs:
                raise ValueError("；".join(errs))
            jdump(obj, f)
            return obj
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
            (out_dir / f"raw_{run.name}_{dm['id']}_{i}.txt").write_text(f"ERR: {err}\n\n{out}", encoding="utf-8")
    raise RuntimeError(f"{run.name} {dm['id']}: scorer failed after {tries + 1} attempts: {err}")


# ---------- utility ----------
def f_value(cfg, row, comp_scale=None) -> float:
    comp_scale = comp_scale or {}
    s = sum(c["weight"] * float(row[c["key"]]) * float(comp_scale.get(c["key"], 1.0))
            for c in cfg["F"]["components"])
    return min(float(cfg["F"]["cap"]), s)


def effective_state(dm, arm_id, sv, vardef) -> dict:
    """var_mods: [{"arms": [...] (optional), "var", "add", "mul"}], result clipped to the variable's range."""
    x = dict(sv)
    for vm in dm.get("var_mods", []):
        if vm.get("arms") and arm_id not in vm["arms"]:
            continue
        v = x.get(vm["var"])
        if v is None:
            continue
        v = v * float(vm.get("mul", 1.0)) + float(vm.get("add", 0.0))
        lo, hi = vardef[vm["var"]]["lo"], vardef[vm["var"]]["hi"]
        x[vm["var"]] = max(lo, min(hi, v))
    return x


def dm_utility(cfg, dm, ai, fval, sv, vardef, expo_scale=1.0, parts=False):
    arm_id = cfg["arms"][ai]["id"]
    x = effective_state(dm, arm_id, sv, vardef)
    x[F_VAR] = fval
    for rl in dm.get("red_lines", []):
        if rl.get("arms") and arm_id not in rl["arms"]:
            continue
        v = x.get(rl["var"])
        if v is not None and OPS[rl["op"]](v, rl["threshold"]):
            return (RED_LINE_PENALTY, {"red_line": rl.get("rule", rl["var"])}) if parts else RED_LINE_PENALTY
    U, p = 0.0, {}
    for t in dm["utility"]:
        var = t["var"]
        if var == F_VAR:
            u = t["weight"] * term_value(t, F_DEF, fval)
        else:
            if x.get(var) is None:
                continue
            u = t["weight"] * exposure(cfg, dm, var, ai, expo_scale) * term_value(t, vardef[var], x[var])
        U += u
        p[var] = p.get(var, 0) + u
    return (U, p) if parts else U


def totals(cfg, dm, obj, rows, vardef, scen=None, override=None) -> dict[str, float]:
    """Discounted total per arm on one trajectory. override(k_index, sv) -> sv lets a sweep replace state."""
    scen = scen or {}
    disc = float(dm.get("discount_per_round", 1.0))
    out = {}
    for ai, a in enumerate(arm_ids(cfg)):
        tot = 0.0
        for k, (r, row) in enumerate(zip(obj["arms"][a], rows)):
            sv = dict(row["state"])
            if override:
                sv = override(k, sv)
            tot += disc ** k * dm_utility(cfg, dm, ai, f_value(cfg, r, scen.get("component_scale")), sv, vardef,
                                          float(scen.get("exposure_scale", 1.0)))
        out[a] = round(tot, 3)
    return out


def rank(per_run: dict[str, dict[str, float]], weights: dict[str, float], arms) -> dict:
    w = {k: weights.get(k, 0) for k in per_run}
    tw = sum(w.values()) or 1
    rows = [{"arm": a, "weighted": round(sum(w[k] * per_run[k][a] for k in per_run) / tw, 3),
             **{k: per_run[k][a] for k in per_run}} for a in arms]
    rows.sort(key=lambda x: -x["weighted"])
    top = {k: max(arms, key=lambda a: per_run[k][a]) for k in per_run}
    return {"ranking": rows, "top_per_run": top,
            "consistent": f"{sum(1 for v in top.values() if v == rows[0]['arm'])}/{len(top)}"}


def decomposition(cfg, dm, objs, trajs, vardef) -> dict:
    """Per arm, averaged over runs and rounds (undiscounted): F, each component, and U − F part."""
    out = {}
    for ai, a in enumerate(arm_ids(cfg)):
        fs, comps, nonf, n = 0.0, {c: 0.0 for c in comp_keys(cfg)}, 0.0, 0
        for run, obj in objs.items():
            for r, row in zip(obj["arms"][a], trajs[run]):
                fv = f_value(cfg, r)
                U, parts = dm_utility(cfg, dm, ai, fv, row["state"], vardef, parts=True)
                fs += fv
                for c in comps:
                    comps[c] += float(r[c])
                nonf += U - (parts.get(F_VAR, 0) if "red_line" not in parts else 0)
                n += 1
        n = n or 1
        out[a] = {"F": round(fs / n, 1), **{c: round(v / n, 1) for c, v in comps.items()},
                  "nonF_U": round(nonf / n, 3)}
    return out


# ---------- top level ----------
def list_runs(base, runs=None) -> list[Path]:
    """Explicit run names, else every layer-1 run (forks excluded: score them with --runs)."""
    base = Path(base)
    return [base / r for r in (runs or runner.layer1_runs(base))]


def score_all(cfg_path, base, runs=None, out_dir=None, call=None, parallel=9) -> dict:
    cfg = load_config(cfg_path)
    run_dirs = list_runs(base, runs)
    if not run_dirs:
        raise FileNotFoundError(f"no runs under {base}")
    _, variables = turn.load_run(str(run_dirs[0]))
    errs, warns = validate(cfg, variables)
    if errs:
        raise ValueError("strategy config: " + "；".join(errs))
    out_dir = Path(out_dir or Path(base) / "layer2")
    out_dir.mkdir(parents=True, exist_ok=True)
    dms = cfg["decision_makers"]
    jobs = [(run, dm) for run in run_dirs for dm in dms]
    with ThreadPoolExecutor(max(1, min(parallel, len(jobs)))) as pool:
        res = dict(zip([(r.name, d["id"]) for r, d in jobs],
                       pool.map(lambda j: score_one(j[0], j[1], cfg, out_dir, call), jobs)))
    trajs = {r.name: trajectory(r, cfg)[0] for r in run_dirs}
    weights = cfg.get("run_weights") or runner.run_weights(base, [r.name for r in run_dirs])
    scens = [{"name": "base"}] + list(cfg["sensitivity"])
    summary = {"weights": weights, "warnings": warns, "by_dm": {},
               "notes": {f"{r}_{d}": o.get("notes") for (r, d), o in res.items()}}
    for dm in dms:
        objs = {r.name: res[(r.name, dm["id"])] for r in run_dirs}
        d = {}
        for sc in scens:
            per = {k: totals(cfg, dm, objs[k], trajs[k], variables, sc) for k in objs}
            d[sc["name"]] = rank(per, weights, arm_ids(cfg))
        base_top2 = [x["arm"] for x in d["base"]["ranking"][:2]]
        for sc in scens[1:]:
            d[sc["name"]]["flip"] = [x["arm"] for x in d[sc["name"]]["ranking"][:2]] != base_top2
        summary["by_dm"][dm["id"]] = {"scenarios": d, "decomposition": decomposition(cfg, dm, objs, trajs, variables)}
    jdump(summary, out_dir / "summary.json")
    md = summary_markdown(cfg, summary)
    (out_dir / "summary.md").write_text(md + "\n", encoding="utf-8")
    summary["markdown"] = md
    return summary


def summary_markdown(cfg, s) -> str:
    names = {a["id"]: a.get("name", "") for a in cfg["arms"]}
    lines = []
    for dm_id, d in s["by_dm"].items():
        b = d["scenarios"]["base"]
        runs = [k for k in b["ranking"][0] if k not in ("arm", "weighted")]
        lines += [f"### {dm_id}", "", "| 臂 | 加权累计 U | " + " | ".join(runs) + " | 平均 F | 非 F 部分 |",
                  "|---|---|" + "---|" * len(runs) + "---|---|"]
        for x in b["ranking"]:
            dc = d["decomposition"][x["arm"]]
            lines.append(f"| {x['arm']} {names.get(x['arm'], '')} | {x['weighted']:.2f} | "
                         + " | ".join(f"{x[k]:.2f}" for k in runs) + f" | {dc['F']} | {dc['nonF_U']:+.3f} |")
        lines.append(f"\n第一名在 {b['consistent']} 条轨迹上一致。敏感性："
                     + "；".join(f"{n} → {v['ranking'][0]['arm']}>{v['ranking'][1]['arm']}"
                                 + ("（前两名翻转）" if v.get("flip") else "")
                                 for n, v in d["scenarios"].items() if n != "base") + "\n")
    if s.get("warnings"):
        lines.append("配置提醒：" + "；".join(s["warnings"]))
    return "\n".join(lines)


def sweep(cfg_path, base, run, from_round: int, grid: dict[str, list[float]], dms=None, out_dir=None) -> dict:
    """Override state vars (grid values) from round from_round on, keep the scorer's F; rank arms per combo.
    Needs the cached scores (run `score` first)."""
    cfg = load_config(cfg_path)
    out_dir = Path(out_dir or Path(base) / "layer2")
    rd = Path(base) / run
    _, variables = turn.load_run(str(rd))
    rows, _ = trajectory(rd, cfg)
    res = {}
    for dm in cfg["decision_makers"]:
        if dms and dm["id"] not in dms:
            continue
        f = out_dir / f"{run}_{dm['id']}.json"
        if not f.exists():
            raise FileNotFoundError(f"{f} missing: run `gamesim score` on {run} first")
        obj = jload(f)
        base_t = totals(cfg, dm, obj, rows, variables)
        base_top = max(base_t, key=base_t.get)
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            ov = dict(zip(keys, combo))

            def override(k, sv, ov=ov):
                return {**sv, **ov} if k >= from_round - 1 else sv
            t = totals(cfg, dm, obj, rows, variables, override=override)
            rk = sorted(t, key=lambda a: -t[a])
            tag = f"{dm['id']} " + " ".join(f"{k}={v:g}" for k, v in ov.items())
            res[tag] = {"ranking": rk, "totals": t, "flip": rk[0] != base_top}
    jdump({"run": run, "from_round": from_round, "grid": grid, "results": res}, out_dir / f"sweep_{run}.json")
    return res
