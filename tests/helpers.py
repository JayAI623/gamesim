"""Shared fixtures for the engine tests. Hermetic: temp dirs only, no network, no model calls."""
import copy
import json
import os
import shutil
from pathlib import Path

from gamesim import turn
from gamesim.target import compute_periods

FIXTURE = Path(__file__).parent / "fixtures" / "hormuz_v3_run1"
HORMUZ_TARGET = {"conds": [{"var": "H", "op": ">=", "value": 95}, {"var": "W", "op": "<=", "value": 30}],
                 "gm_flags": ["agreement.in_effect"], "kind": "ever", "min_rounds": None}

ENT = {"name": "甲", "codename": "Alpha", "weight": 0.5, "members": "", "private_info": "秘密A",
       "capabilities": "兵力 10 万", "time_preference": "short", "risk": 0.0,
       "base_rates": [{"pattern": "报复", "rate": "3/4"}],
       "utility": [{"var": "H", "weight": 1.0, "direction": "+", "shape": "linear"}],
       "red_lines": [{"rule": "H 归零", "var": "H", "op": "<=", "threshold": 5}],
       "bdm": {"position": 30, "capability": 50, "salience": 0.8, "risk": 0.0}}
ENT2 = {**ENT, "name": "乙", "codename": "Bravo", "private_info": "秘密B", "time_preference": "long",
        "bdm": {"position": 70, "capability": 40, "salience": 0.6, "risk": 0.2}}


def small_entities(settings=None, *, agreement=True, target=True, periods=True, rounds=2):
    data = {"question": "Q?", "settings": {"rounds": rounds, "time_per_round": "1个月", "seed": 7, **(settings or {})},
            "variables": [{"sym": "H", "name": "流量", "lo": 0, "hi": 100, "current": 80},
                          {"sym": "W", "name": "烈度", "lo": 0, "hi": 100, "current": 50}],
            "entities": [copy.deepcopy(ENT), copy.deepcopy(ENT2)], "issue": {"low": "a", "high": "b"},
            "action_menu": [{"level": 0, "action": "让步"}, {"level": 1, "action": "观望"}],
            "initial_public_state": "开局", "exogenous": ["选举"]}
    if agreement:
        data["agreement"] = {"name": "停火", "parties": "甲与乙", "context": "条件X"}
    if target:
        data["target"] = copy.deepcopy(HORMUZ_TARGET)
        if not agreement:
            data["target"]["gm_flags"] = []
    if periods:
        data["periods"] = compute_periods("2026-11-03", "2027-01-02", rounds)
    return data


def write_entities(d, data):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "entities.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def fixture_entities(v08=True):
    data = json.loads((FIXTURE / "entities.json").read_text(encoding="utf-8"))
    if v08:
        data["target"] = copy.deepcopy(HORMUZ_TARGET)
        data["periods"] = compute_periods("2026-11-03", "2027-04-30", data["settings"]["rounds"])
        data["agreement"] = {"name": "停火", "parties": "美国与伊朗（IRGC 核心 + 外交线）",
                             "context": data.pop("ceasefire_context")}
    return data


def move(esc=3, diary="日记"):
    return {"options_considered": [], "chosen_index": 0, "chosen": {"action": "谈", "escalation": esc},
            "position_now": 50, "diary": diary}


def gm_out(H=80, W=50, in_effect=False, terminal=False, **extra):
    return {"state_vars": {"H": H, "W": W}, "public_state": "公开", "last_round_public": "上轮",
            "agreement": {"in_effect": in_effect}, "p_target": 0.3, "terminal": terminal, **extra}


def dump(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def play_round(run, r, gm, moves=None, io="stdout", events=None, attempt=0):
    """prep -> moves -> gm [-> commit -> roll] -> gm.json -> apply, for the small 2-entity scenario."""
    turn.cmd_prep(run, r, io, events)
    d = os.path.join(run, "turns", f"r{r:02d}")
    for code in ("Alpha", "Bravo"):
        dump((moves or {}).get(code, move()), os.path.join(d, f"move_{code}.json"))
    res = turn.cmd_gm(run, r, io)
    if res["phase"] == "commit":
        dump({"probs": {"甲": {"p_gm": 0.5}, "乙": {"p_gm": 0.5}}}, os.path.join(d, "gm_commit.json"))
        turn.cmd_roll(run, r, io)
    dump(gm, os.path.join(d, "gm.json"))
    return turn.cmd_apply(run, r, attempt)


def copy_fixture_files(run, r, *patterns):
    """Copy fixture turns/rNN files matching glob patterns (e.g. 'move_*.json', 'gm.json') into the run."""
    src, dst = FIXTURE / "turns" / f"r{r:02d}", Path(run) / "turns" / f"r{r:02d}"
    dst.mkdir(parents=True, exist_ok=True)
    for pat in patterns:
        for f in src.glob(pat):
            shutil.copy(f, dst / f.name)
