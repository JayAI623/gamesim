"""Opening-review conditions: JSON Pointers, coverage check, per-condition patches.

conditions.json = [{"cid": "c01", "grp": "<codename>|global", "sentence": "≤60 字", "paths": ["/entities/0/utility"],
                    "sources": ["…"], "confidence": "高|中|低"}]

A condition path P covers a required path R when P equals R or is an ancestor of R
(e.g. "/entities/0/bdm" covers "/entities/0/bdm/risk"). Paths of different conditions may not be equal or
ancestor/descendant of each other, so a patch locked to one condition can never touch another.
"""
from __future__ import annotations

import copy
import re

MAX_CHARS = 60
CONFIDENCE = ("高", "中", "低")
CID_RE = re.compile(r"^c\d{2,3}$")
BDM_KEYS = ("position", "capability", "salience", "risk")


# ---------- JSON Pointer (RFC 6901) ----------
def parse_pointer(ptr: str) -> list[str]:
    if not isinstance(ptr, str) or not ptr.startswith("/"):
        raise ValueError(f"JSON Pointer must start with '/': {ptr!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr[1:].split("/")]


def make_pointer(tokens) -> str:
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


def _step(cur, tok):
    if isinstance(cur, dict):
        if tok not in cur:
            raise KeyError(tok)
        return cur[tok]
    if isinstance(cur, list):
        if not tok.isdigit() or (len(tok) > 1 and tok[0] == "0") or int(tok) >= len(cur):
            raise KeyError(tok)
        return cur[int(tok)]
    raise KeyError(tok)


def pointer_get(doc, ptr: str):
    cur = doc
    for tok in parse_pointer(ptr):
        cur = _step(cur, tok)
    return cur


def pointer_exists(doc, ptr: str) -> bool:
    try:
        pointer_get(doc, ptr)
        return True
    except (KeyError, ValueError):
        return False


def is_prefix(a: str, b: str) -> bool:
    """True if pointer a equals b or is an ancestor of b (token-wise)."""
    ta, tb = parse_pointer(a), parse_pointer(b)
    return len(ta) <= len(tb) and tb[:len(ta)] == ta


def apply_patch(doc, ops: list[dict]):
    """Apply JSON-Patch-style ops (replace / add / remove) to a deep copy of doc and return it."""
    out = copy.deepcopy(doc)
    for op in ops:
        kind, toks = op.get("op"), parse_pointer(op.get("path", ""))
        parent = out
        for tok in toks[:-1]:
            parent = _step(parent, tok)
        last = toks[-1]
        if kind == "replace":
            _step(parent, last)
            if isinstance(parent, list):
                parent[int(last)] = copy.deepcopy(op["value"])
            else:
                parent[last] = copy.deepcopy(op["value"])
        elif kind == "add":
            if isinstance(parent, list):
                idx = len(parent) if last == "-" else int(last)
                if not 0 <= idx <= len(parent):
                    raise KeyError(last)
                parent.insert(idx, copy.deepcopy(op["value"]))
            elif isinstance(parent, dict):
                parent[last] = copy.deepcopy(op["value"])
            else:
                raise KeyError(last)
        elif kind == "remove":
            _step(parent, last)
            if isinstance(parent, list):
                del parent[int(last)]
            else:
                del parent[last]
        else:
            raise ValueError(f"unsupported patch op {kind!r}")
    return out


def patch_within(ops: list[dict], allowed_paths: list[str]) -> list[str]:
    """Errors for patch ops whose path is not equal to / below one of the condition's paths."""
    err = []
    for k, op in enumerate(ops if isinstance(ops, list) else []):
        p = op.get("path") if isinstance(op, dict) else None
        try:
            ok = any(is_prefix(a, p) for a in allowed_paths)
        except ValueError:
            ok = False
        if not ok:
            err.append(f"patch[{k}] 的 path {p!r} 不在这一条的 paths 之内")
    if not isinstance(ops, list):
        err.append("patch 必须是数组")
    return err


# ---------- coverage ----------
def required_paths(entities: dict) -> list[str]:
    """Every pointer that some condition must cover: per actor goals(utility, incl. weights), red_lines,
    capabilities, private_info, the 4 bdm params; globally each state var start value, action_menu, periods."""
    req = []

    def each_or_whole(base: str, xs):
        if isinstance(xs, list) and xs:
            req.extend(f"{base}/{j}" for j in range(len(xs)))
        else:
            req.append(base)

    for i, e in enumerate(entities.get("entities") or []):
        b = f"/entities/{i}"
        each_or_whole(f"{b}/utility", e.get("utility"))
        each_or_whole(f"{b}/red_lines", e.get("red_lines"))
        if "capabilities" in e:
            req.append(f"{b}/capabilities")
        req.append(f"{b}/private_info")
        req.extend(f"{b}/bdm/{k}" for k in BDM_KEYS)
    for k in range(len(entities.get("variables") or [])):
        req.append(f"/variables/{k}/current")
    each_or_whole("/action_menu", entities.get("action_menu"))
    each_or_whole("/periods", entities.get("periods"))
    return req


def _label(entities: dict, ptr: str) -> str:
    toks = parse_pointer(ptr)
    try:
        if toks[0] == "entities":
            name = entities["entities"][int(toks[1])].get("name", toks[1])
            return f"{name} " + ".".join(toks[2:])
        if toks[0] == "variables":
            return f"变量 {entities['variables'][int(toks[1])].get('sym', toks[1])} 的起点"
    except (IndexError, KeyError, ValueError):
        pass
    return ".".join(toks)


def coverage_check(entities: dict, conditions, *, max_chars: int = MAX_CHARS) -> list[str]:
    """Shape + coverage check of conditions.json against entities.json. Returns error strings (empty = ok)."""
    if isinstance(conditions, dict) and "conditions" in conditions:
        conditions = conditions["conditions"]
    if not isinstance(conditions, list) or not conditions:
        return ["conditions 必须是非空数组"]
    ents = entities.get("entities") or []
    code_idx = {e.get("codename"): i for i, e in enumerate(ents) if isinstance(e, dict)}
    err: list[str] = []
    seen_cid = set()
    owned: list[tuple[str, str]] = []  # (cid, path) for valid paths
    for k, c in enumerate(conditions):
        if not isinstance(c, dict):
            err.append(f"conditions[{k}] 必须是对象")
            continue
        cid = c.get("cid")
        tag = cid if isinstance(cid, str) else f"conditions[{k}]"
        if not (isinstance(cid, str) and CID_RE.fullmatch(cid)):
            err.append(f"{tag}: cid 必须形如 c01")
        elif cid in seen_cid:
            err.append(f"{tag}: cid 重复")
        seen_cid.add(cid)
        grp = c.get("grp")
        if grp != "global" and grp not in code_idx:
            err.append(f"{tag}: grp {grp!r} 必须是某个参与方的 codename 或 global")
        s = c.get("sentence")
        if not (isinstance(s, str) and s.strip()):
            err.append(f"{tag}: sentence 必须是非空字符串")
        elif len(s) > max_chars:
            err.append(f"{tag}: sentence 有 {len(s)} 字，超过 {max_chars} 字")
        if c.get("confidence") not in CONFIDENCE:
            err.append(f"{tag}: confidence 必须是 高/中/低")
        if not isinstance(c.get("sources", []), list):
            err.append(f"{tag}: sources 必须是数组")
        paths = c.get("paths")
        if not (isinstance(paths, list) and paths):
            err.append(f"{tag}: paths 必须是非空数组")
            continue
        for p in paths:
            if not isinstance(p, str) or not p.startswith("/") or p == "/":
                err.append(f"{tag}: path {p!r} 不是 JSON Pointer（形如 /entities/0/bdm）")
                continue
            if not pointer_exists(entities, p):
                err.append(f"{tag}: path {p} 在 entities.json 里不存在")
                continue
            toks = parse_pointer(p)
            if grp in code_idx and not (toks[0] == "entities" and len(toks) > 1 and toks[1] == str(code_idx[grp])):
                err.append(f"{tag}: grp={grp}，但 path {p} 不在 /entities/{code_idx[grp]} 下")
            if grp == "global" and toks[0] == "entities":
                err.append(f"{tag}: grp=global 的条不能引用参与方的 path {p}")
            if len(toks) < 2 and toks[0] in ("entities", "variables"):
                err.append(f"{tag}: path {p} 太宽，会覆盖整个 {toks[0]}")
            owned.append((tag, p))
    for a in range(len(owned)):
        for b in range(a + 1, len(owned)):
            (ca, pa), (cb, pb) = owned[a], owned[b]
            if ca == cb:
                continue
            if pa == pb:
                err.append(f"path {pa} 同时出现在 {ca} 和 {cb}")
            elif is_prefix(pa, pb) or is_prefix(pb, pa):
                err.append(f"{ca} 的 {pa} 和 {cb} 的 {pb} 重叠")
    for r in required_paths(entities):
        if not pointer_exists(entities, r):
            err.append(f"entities.json 缺少 {r}")
        elif not any(is_prefix(p, r) for _, p in owned):
            err.append(f"没有条件覆盖 {r}（{_label(entities, r)}）")
    return err


def sort_conditions(conditions: list[dict]) -> list[dict]:
    """Group in order of first appearance; inside each group 低 → 中 → 高 (stable)."""
    order: dict[str, int] = {}
    for c in conditions:
        order.setdefault(c.get("grp"), len(order))
    rank = {"低": 0, "中": 1, "高": 2}
    return sorted(conditions, key=lambda c: (order[c.get("grp")], rank.get(c.get("confidence"), 3)))
