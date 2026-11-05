#!/usr/bin/env python3
"""Ingest `claude -p` stdout into the files turn.py expects, then validate.

Round outputs (written into <run>/turns/rNN/):
  ingest.py round <run> <r> move <codename>   < stdout.txt   -> move_<codename>.json
  ingest.py round <run> <r> gm_commit         < stdout.txt   -> gm_commit.json
  ingest.py round <run> <r> gm                < stdout.txt   -> gm.json
  ingest.py fallback <run> <r> <codename> [reason]           -> move_<codename>.json = 本轮观望 (escalation 1)
Setup outputs (written into the gamesim run dir):
  ingest.py setup <run_dir> entities|conditions|result_summary < stdout.txt

Exit 0 and print the written path; exit 2 and print the error text (meant to be appended to the retry prompt).
"""
from __future__ import annotations

import argparse
import sys

try:
    from . import schema
    from .conditions import coverage_check, sort_conditions
    from .fsio import PathError, check_codename, extract_json, jdump, jload, safe_path
    from .turn import GM_KEYS, load_run, move_path, turn_file, validate_move
except ImportError:  # run as a script
    import schema
    from conditions import coverage_check, sort_conditions
    from fsio import PathError, check_codename, extract_json, jdump, jload, safe_path
    from turn import GM_KEYS, load_run, move_path, turn_file, validate_move

ROUND_KINDS = ("move", "gm_commit", "gm")
SETUP_KINDS = {"entities": "entities.json", "conditions": "conditions.json", "result_summary": "result_summary.json"}


class IngestError(ValueError):
    """Model output that cannot be used; str(e) is a Chinese explanation for the retry prompt."""


def parse(text: str, want=dict):
    try:
        obj = extract_json(text)
    except ValueError:
        raise IngestError("输出里没有找到合法的 JSON。只输出一个 ```json 代码块。") from None
    if not isinstance(obj, want):
        raise IngestError(f"JSON 顶层必须是{'对象' if want is dict else '数组'}。")
    return obj


def _p(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1


def check_gm_commit(obj, data) -> list[str]:
    probs = obj.get("probs")
    if not isinstance(probs, dict):
        return ["缺少 probs 对象"]
    err = []
    for e in data["entities"]:
        c = probs.get(e["name"])
        if not isinstance(c, dict):
            err.append(f"probs 缺少参与方「{e['name']}」")
        elif not _p(c.get("p_gm")):
            err.append(f"probs.{e['name']}.p_gm 必须是 0–1 的数字")
    for k, x in enumerate(obj.get("exogenous") or []):
        if not (isinstance(x, dict) and _p(x.get("p"))):
            err.append(f"exogenous[{k}].p 必须是 0–1 的数字")
    return err


def check_gm(obj, data) -> list[str]:
    err = [f"缺少字段 {k}" for k in GM_KEYS if k not in obj]
    if "state_vars" in obj and not isinstance(obj["state_vars"], dict):
        err.append("state_vars 必须是对象")
    for f in (data.get("target") or {}).get("gm_flags") or []:
        cur = obj
        for key in f.split("."):
            cur = cur.get(key) if isinstance(cur, dict) else None
        if not isinstance(cur, bool):
            err.append(f"必须给出布尔字段 {f}（true 或 false）")
    if "p_target" in obj and obj["p_target"] is not None and not _p(obj["p_target"]):
        err.append("p_target 必须是 0–1 的数字")
    return err


def ingest_round(run, r: int, kind: str, text: str, code: str | None = None) -> dict:
    """Parse + validate one round output and write it atomically. Returns {"path", "obj"}."""
    if kind not in ROUND_KINDS:
        raise ValueError(f"kind must be one of {ROUND_KINDS}")
    data, _ = load_run(run)
    obj = parse(text)
    if kind == "move":
        check_codename(code)
        if code not in {e["codename"] for e in data["entities"]}:
            raise PathError(f"unknown codename {code}")
        miss = validate_move(obj)
        if miss:
            raise IngestError(f"缺少字段：{', '.join(miss)}")
        path = move_path(run, r, code)
    elif kind == "gm_commit":
        err = check_gm_commit(obj, data)
        if err:
            raise IngestError("；".join(err))
        path = turn_file(run, r, "gm_commit.json")
    else:
        err = check_gm(obj, data)
        if err:
            raise IngestError("；".join(err))
        path = turn_file(run, r, "gm.json")
    jdump(obj, path)
    return {"path": str(path), "obj": obj}


def fallback_move(name: str, reason: str = "") -> dict:
    """After the retries fail, the role is recorded as 本轮观望 (escalation 1)."""
    return {"entity": name, "options_considered": [], "chosen_index": 0,
            "chosen": {"action": "维持现状/观望", "target": "all", "escalation": 1, "public_statement": ""},
            "position_now": None, "deviation_reason": "", "diary": "", "fallback": True, "fallback_reason": reason}


def write_fallback_move(run, r: int, code: str, reason: str = "") -> dict:
    data, _ = load_run(run)
    ent = next((e for e in data["entities"] if e["codename"] == check_codename(code)), None)
    if ent is None:
        raise PathError(f"unknown codename {code}")
    obj = fallback_move(ent["name"], reason)
    path = move_path(run, r, code)
    jdump(obj, path)
    return {"path": str(path), "obj": obj}


def ingest_setup(run_dir, kind: str, text: str, *, entities: dict | None = None, target=None, periods=None,
                 rounds=None, seed=None) -> dict:
    """assemble -> entities.json (caller-owned fields pinned, then validated);
    conditions -> conditions.json (coverage_check against entities, then sorted 低 first per group);
    result_summary -> result_summary.json. Raises IngestError with every problem."""
    if kind not in SETUP_KINDS:
        raise ValueError(f"kind must be one of {tuple(SETUP_KINDS)}")
    path = safe_path(run_dir, SETUP_KINDS[kind])
    if kind == "entities":
        obj = parse(text)
        schema.finalize_assembled(obj, target=target, periods=periods, rounds=rounds, seed=seed)
        err = schema.validate_entities(obj)
    elif kind == "conditions":
        obj = extract_json_any(text)
        if isinstance(obj, dict) and isinstance(obj.get("conditions"), list):
            obj = obj["conditions"]
        if entities is None:
            entities = jload(safe_path(run_dir, "entities.json"))
        err = coverage_check(entities, obj)
        if not err:
            obj = sort_conditions(obj)
    else:
        obj = parse(text)
        err = schema.validate_result_summary(obj)
    if err:
        raise IngestError("；".join(err))
    jdump(obj, path)
    return {"path": str(path), "obj": obj}


def extract_json_any(text):
    try:
        return extract_json(text)
    except ValueError:
        raise IngestError("输出里没有找到合法的 JSON。只输出一个 ```json 代码块。") from None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    rp = sub.add_parser("round")
    rp.add_argument("run")
    rp.add_argument("round", type=int)
    rp.add_argument("kind", choices=ROUND_KINDS)
    rp.add_argument("code", nargs="?")
    fp = sub.add_parser("fallback")
    fp.add_argument("run")
    fp.add_argument("round", type=int)
    fp.add_argument("code")
    fp.add_argument("reason", nargs="?", default="")
    sp = sub.add_parser("setup")
    sp.add_argument("run_dir")
    sp.add_argument("kind", choices=tuple(SETUP_KINDS))
    a = ap.parse_args(argv)
    try:
        if a.mode == "round":
            res = ingest_round(a.run, a.round, a.kind, sys.stdin.read(), a.code)
        elif a.mode == "fallback":
            res = write_fallback_move(a.run, a.round, a.code, a.reason)
        else:
            res = ingest_setup(a.run_dir, a.kind, sys.stdin.read())
    except (IngestError, PathError, ValueError, FileNotFoundError) as ex:
        print(str(ex))
        return 2
    print(res["path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
