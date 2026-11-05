"""Schema-ish validators for entities.json, user events and result_summary output.

Each validator returns a list of human-readable error strings (empty = valid), so the caller can
append them to a retry prompt verbatim.
"""
from __future__ import annotations

import re
from datetime import date

try:
    from . import target as tg
    from .fsio import CODENAME_RE
except ImportError:  # run as a script
    import target as tg
    from fsio import CODENAME_RE

SHAPES = ("linear", "concave", "convex", "threshold", "peak")
RED_OPS = (">=", "<=", ">", "<")
SYM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
FLAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
MIN_ENTITIES, MAX_ENTITIES = 2, 8
WEIGHT_TOL = 0.02
SUMMARY_MAX_ITEMS, SUMMARY_MAX_CHARS = 3, 80


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _str(x) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _date(x) -> date | None:
    try:
        return date.fromisoformat(x) if isinstance(x, str) else None
    except ValueError:
        return None


def validate_entities(data, *, require_target: bool = True, require_periods: bool = True) -> list[str]:
    """Validate an entities.json dict. Old (pre-v0.8) runs: pass require_target=False, require_periods=False."""
    if not isinstance(data, dict):
        return ["entities.json 顶层必须是 JSON 对象"]
    err: list[str] = []
    if not _str(data.get("question")):
        err.append("question 必须是非空字符串")
    issue = data.get("issue")
    if not (isinstance(issue, dict) and _str(issue.get("low")) and _str(issue.get("high"))):
        err.append("issue 必须是 {low, high} 两个非空字符串")

    st = data.get("settings")
    rounds = None
    if not isinstance(st, dict):
        err.append("settings 必须是对象")
    else:
        rounds = st.get("rounds")
        if not (isinstance(rounds, int) and not isinstance(rounds, bool) and 1 <= rounds <= 12):
            err.append("settings.rounds 必须是 1–12 的整数")
            rounds = None
        if not _str(st.get("time_per_round")):
            err.append("settings.time_per_round 必须是非空字符串")
        if "seed" in st and not (isinstance(st["seed"], int) and not isinstance(st["seed"], bool)):
            err.append("settings.seed 必须是整数")

    # variables
    syms: dict[str, dict] = {}
    vs = data.get("variables")
    if not (isinstance(vs, list) and vs):
        err.append("variables 必须是非空数组")
        vs = []
    for k, v in enumerate(vs):
        at = f"variables[{k}]"
        if not isinstance(v, dict):
            err.append(f"{at} 必须是对象")
            continue
        sym = v.get("sym")
        if not (isinstance(sym, str) and SYM_RE.fullmatch(sym)):
            err.append(f"{at}.sym 必须是字母开头的短符号")
            continue
        if sym in syms:
            err.append(f"{at}.sym {sym} 重复")
        if not _str(v.get("name")):
            err.append(f"{at}.name 必须是非空字符串")
        lo, hi, cur = v.get("lo"), v.get("hi"), v.get("current")
        if not (_num(lo) and _num(hi) and lo < hi):
            err.append(f"{at} 需要数字 lo < hi")
        elif not (_num(cur) and lo <= cur <= hi):
            err.append(f"{at}.current 必须是 [{lo}, {hi}] 内的数字")
        syms[sym] = v

    # action menu
    menu = data.get("action_menu")
    if not (isinstance(menu, list) and menu):
        err.append("action_menu 必须是非空数组")
    else:
        for k, a in enumerate(menu):
            if not (isinstance(a, dict) and _num(a.get("level")) and _str(a.get("action"))):
                err.append(f"action_menu[{k}] 必须是 {{level: 数字, action: 非空字符串}}")

    if not _str(data.get("initial_public_state")):
        err.append("initial_public_state 必须是非空字符串")

    # entities
    ents = data.get("entities")
    if not isinstance(ents, list) or not (MIN_ENTITIES <= len(ents) <= MAX_ENTITIES):
        err.append(f"entities 必须是 {MIN_ENTITIES}–{MAX_ENTITIES} 个元素的数组")
        ents = ents if isinstance(ents, list) else []
    names, codes, wsum = set(), set(), 0.0
    for i, e in enumerate(ents):
        at = f"entities[{i}]"
        if not isinstance(e, dict):
            err.append(f"{at} 必须是对象")
            continue
        nm = e.get("name")
        if not _str(nm):
            err.append(f"{at}.name 必须是非空字符串")
        elif nm in names:
            err.append(f"{at}.name {nm} 重复")
        names.add(nm)
        code = e.get("codename")
        if not (isinstance(code, str) and CODENAME_RE.fullmatch(code)):
            err.append(f"{at}.codename {code!r} 必须匹配 ^[A-Z][a-z]{{2,15}}$")
        elif code in codes:
            err.append(f"{at}.codename {code} 重复")
        codes.add(code)
        w = e.get("weight")
        if not (_num(w) and 0 <= w <= 1):
            err.append(f"{at}.weight 必须是 0–1 的数字")
        else:
            wsum += w
        if not isinstance(e.get("private_info"), str):
            err.append(f"{at}.private_info 必须是字符串")
        if "capabilities" in e and not isinstance(e["capabilities"], str):
            err.append(f"{at}.capabilities 必须是字符串")
        if "risk" in e and not (_num(e["risk"]) and -1 <= e["risk"] <= 1):
            err.append(f"{at}.risk 必须在 [-1, 1]")
        if "time_preference" in e and e["time_preference"] not in ("short", "long"):
            err.append(f"{at}.time_preference 必须是 short 或 long")
        ut = e.get("utility")
        if not (isinstance(ut, list) and ut):
            err.append(f"{at}.utility 必须是非空数组")
            ut = []
        usum = 0.0
        for j, t in enumerate(ut):
            tat = f"{at}.utility[{j}]"
            if not isinstance(t, dict):
                err.append(f"{tat} 必须是对象")
                continue
            if t.get("var") not in syms:
                err.append(f"{tat}.var {t.get('var')!r} 不在 variables 里")
            if not (_num(t.get("weight")) and t["weight"] >= 0):
                err.append(f"{tat}.weight 必须是非负数字")
            else:
                usum += t["weight"]
            if t.get("direction", "+") not in ("+", "-"):
                err.append(f"{tat}.direction 必须是 + 或 -")
            shape = t.get("shape", "linear")
            if shape not in SHAPES:
                err.append(f"{tat}.shape 必须是 {'/'.join(SHAPES)} 之一")
            if shape in ("threshold", "peak") and not _num(t.get("threshold")):
                err.append(f"{tat}: shape={shape} 需要数字 threshold")
        if ut and abs(usum - 1) > WEIGHT_TOL:
            err.append(f"{at}（{nm}）utility 权重之和是 {usum:.3f}，应为 1")
        rls = e.get("red_lines", [])
        if not isinstance(rls, list):
            err.append(f"{at}.red_lines 必须是数组")
            rls = []
        for j, rl in enumerate(rls):
            rat = f"{at}.red_lines[{j}]"
            if not (isinstance(rl, dict) and _str(rl.get("rule"))):
                err.append(f"{rat} 必须是带 rule 的对象")
                continue
            if rl.get("var") not in syms:
                err.append(f"{rat}.var {rl.get('var')!r} 不在 variables 里")
            if rl.get("op") not in RED_OPS:
                err.append(f"{rat}.op 必须是 {'/'.join(RED_OPS)} 之一")
            if not _num(rl.get("threshold")):
                err.append(f"{rat}.threshold 必须是数字")
        b = e.get("bdm")
        if not isinstance(b, dict):
            err.append(f"{at}.bdm 必须是对象")
        else:
            if not (_num(b.get("position")) and 0 <= b["position"] <= 100):
                err.append(f"{at}.bdm.position 必须在 [0, 100]")
            if not (_num(b.get("capability")) and b["capability"] > 0):
                err.append(f"{at}.bdm.capability 必须 > 0")
            if not (_num(b.get("salience")) and 0 <= b["salience"] <= 1):
                err.append(f"{at}.bdm.salience 必须在 [0, 1]")
            if not (_num(b.get("risk")) and -1 <= b["risk"] <= 1):
                err.append(f"{at}.bdm.risk 必须在 [-1, 1]")
    if ents and abs(wsum - 1) > WEIGHT_TOL:
        err.append(f"各方 weight 之和是 {wsum:.3f}，应为 1")

    # agreement
    ag = data.get("agreement")
    if ag is not None and not (isinstance(ag, dict) and _str(ag.get("name"))):
        err.append("agreement 要么省略，要么是带非空 name 的对象")

    # target
    t = data.get("target")
    if t is None:
        if require_target:
            err.append("缺少 target")
    elif not isinstance(t, dict):
        err.append("target 必须是对象")
    else:
        conds, flags = t.get("conds", []), t.get("gm_flags", [])
        if not isinstance(conds, list) or not isinstance(flags, list) or not (conds or flags):
            err.append("target 需要 conds 和/或 gm_flags（数组，至少一项）")
            conds, flags = [], []
        for k, c in enumerate(conds):
            if not (isinstance(c, dict) and c.get("var") in syms and c.get("op") in tg.OPS and _num(c.get("value"))):
                err.append(f"target.conds[{k}] 必须是 {{var: 已有变量, op: {'/'.join(tg.OPS)}, value: 数字}}")
        for k, f in enumerate(flags):
            if not (isinstance(f, str) and FLAG_RE.fullmatch(f)):
                err.append(f"target.gm_flags[{k}] 必须是点路径，例如 agreement.in_effect")
            elif f.split(".")[0] == "agreement" and not isinstance(ag, dict):
                err.append(f"target.gm_flags 引用了 {f}，但没有 agreement")
        kind = t.get("kind", "ever")
        if kind not in tg.KINDS:
            err.append(f"target.kind 必须是 {'/'.join(tg.KINDS)} 之一")
        if kind == "sustained":
            mr = t.get("min_rounds")
            if not (isinstance(mr, int) and not isinstance(mr, bool) and 1 <= mr <= (rounds or 12)):
                err.append("kind=sustained 需要 1..rounds 的整数 min_rounds")

    # periods
    ps = data.get("periods")
    if ps is None:
        if require_periods:
            err.append("缺少 periods")
    elif not isinstance(ps, list) or (rounds is not None and len(ps) != rounds):
        err.append(f"periods 必须是长度为 settings.rounds（{rounds}）的数组")
    else:
        prev_end = None
        for k, p in enumerate(ps):
            s, e = (_date(p.get("start")), _date(p.get("end"))) if isinstance(p, dict) else (None, None)
            if not (isinstance(p, dict) and p.get("round") == k + 1 and s and e and s <= e):
                err.append(f"periods[{k}] 必须是 {{round: {k + 1}, start <= end, YYYY-MM-DD}}")
                prev_end = None
                continue
            if prev_end and s < prev_end:
                err.append(f"periods[{k}] 和上一轮重叠或倒序")
            prev_end = e
    return err


def validate_events(events) -> list[str]:
    """User events: [{id: str, seq: int, text: str}] (extra keys allowed)."""
    if not isinstance(events, list):
        return ["events 必须是数组"]
    err, ids = [], set()
    for k, ev in enumerate(events):
        if not isinstance(ev, dict):
            err.append(f"events[{k}] 必须是对象")
            continue
        if not _str(ev.get("id")):
            err.append(f"events[{k}].id 必须是非空字符串")
        elif ev["id"] in ids:
            err.append(f"events[{k}].id {ev['id']} 重复")
        ids.add(ev.get("id"))
        if not (isinstance(ev.get("seq"), int) and not isinstance(ev.get("seq"), bool)):
            err.append(f"events[{k}].seq 必须是整数")
        if not _str(ev.get("text")):
            err.append(f"events[{k}].text 必须是非空字符串")
    return err


def validate_result_summary(obj, *, max_items: int = SUMMARY_MAX_ITEMS,
                            max_chars: int = SUMMARY_MAX_CHARS) -> list[str]:
    """result_summary output: {"bottlenecks": [str], "signposts": [str with a threshold number], "invalidators": [str]}."""
    if not isinstance(obj, dict):
        return ["result_summary 必须是 JSON 对象"]
    err = []
    for key in ("bottlenecks", "signposts", "invalidators"):
        xs = obj.get(key)
        if not (isinstance(xs, list) and 1 <= len(xs) <= max_items):
            err.append(f"{key} 必须是 1–{max_items} 条的数组")
            continue
        for k, x in enumerate(xs):
            if not _str(x):
                err.append(f"{key}[{k}] 必须是非空字符串")
            elif len(x) > max_chars:
                err.append(f"{key}[{k}] 有 {len(x)} 字，超过 {max_chars} 字")
            elif key == "signposts" and not re.search(r"\d", x):
                err.append(f"signposts[{k}] 必须带阈值数字")
    return err


def finalize_assembled(data: dict, *, target: dict | None = None, periods: list | None = None,
                       rounds: int | None = None, seed: int | None = None) -> dict:
    """Pin the fields the caller owns onto an LLM-assembled entities.json (in place; returns it).
    target / periods are frozen at confirm time and must not be re-invented by assemble."""
    st = data.setdefault("settings", {})
    if rounds is not None:
        st["rounds"] = rounds
    for k in ("blind_dice", "base_rate_cap", "reference_class", "discount"):
        st.setdefault(k, True)
    st.setdefault("models", {})
    if seed is not None:
        st["seed"] = seed
    if target is not None:
        data["target"] = target
    if periods is not None:
        data["periods"] = periods
    return data
