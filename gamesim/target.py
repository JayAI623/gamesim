"""Target condition, question kinds, round periods and seeds.

entities.json carries
  "target":  {"conds": [{"var": "H", "op": ">=", "value": 95}], "gm_flags": ["agreement.in_effect"],
              "kind": "ever|at_deadline|sustained", "min_rounds": null}
  "periods": [{"round": 1, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, ...]
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
       "<": lambda a, b: a < b, "==": lambda a, b: a == b}
OP_TEXT = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "="}
KINDS = ("ever", "at_deadline", "sustained")


# ---------- hit ----------
def get_flag(obj, dotted: str):
    cur = obj
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "是"))


def eval_target(target: dict | None, state_vars: dict, gm: dict) -> bool:
    """hit = every cond holds on the post-round state AND every gm_flag dot-path is true in the GM output."""
    if not target:
        return False
    conds = target.get("conds") or []
    flags = target.get("gm_flags") or []
    if not conds and not flags:
        return False
    for c in conds:
        v = state_vars.get(c.get("var"))
        op = OPS.get(c.get("op"))
        if v is None or op is None:
            return False
        try:
            if not op(float(v), float(c["value"])):
                return False
        except (TypeError, ValueError, KeyError):
            return False
    return all(truthy(get_flag(gm, f)) for f in flags)


def hist_hit(h: dict) -> bool:
    """Read hit from a history entry; `recovered` is the pre-v0.8 alias."""
    return bool(h.get("hit", h.get("recovered", False)))


def longest_streak(hits: list[bool]) -> int:
    best = cur = 0
    for x in hits:
        cur = cur + 1 if x else 0
        best = max(best, cur)
    return best


def run_hit(hits: list[bool], kind: str, min_rounds: int | None = None) -> bool:
    """Per-run hit for the question kind. `hits` = per committed round (absorbed rounds are not appended):
    ever / sustained count the rounds actually played; at_deadline uses the last played round (absorbing state)."""
    if kind == "ever":
        return any(hits)
    if kind == "at_deadline":
        return bool(hits) and bool(hits[-1])
    if kind == "sustained":
        if not min_rounds or min_rounds < 1:
            raise ValueError("sustained needs min_rounds >= 1")
        return longest_streak(hits) >= min_rounds
    raise ValueError(f"unknown kind {kind!r}")


# ---------- rendering ----------
def render_conds(target: dict, variables: dict) -> str:
    parts = []
    for c in target.get("conds") or []:
        v = variables.get(c.get("var"), {})
        name = f"（{v['name']}）" if v.get("name") else ""
        parts.append(f"{c.get('var')} {OP_TEXT.get(c.get('op'), c.get('op'))} {c.get('value')}{name}")
    for f in target.get("gm_flags") or []:
        parts.append(f"GM 输出里 `{f}` 为 true")
    return "，且 ".join(parts) if parts else "（未定义）"


def deadline_text(kind: str, min_rounds, resolve_on: str | None, rounds: int) -> str:
    until = f"到 {resolve_on} 为止" if resolve_on else f"到第 {rounds} 轮末为止"
    if kind == "at_deadline":
        when = f"{resolve_on} 那天" if resolve_on else f"第 {rounds} 轮末"
        return f"{when}（最后一轮末）处于目标状态"
    if kind == "sustained":
        return f"{until}，连续至少 {min_rounds} 轮的轮末都处于目标状态"
    return f"{until}，任意一轮的轮末达到过目标状态"


# ---------- periods ----------
def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def months_between(start, end) -> float:
    s, e = _d(start), _d(end)
    return (e.year - s.year) * 12 + (e.month - s.month) + (e.day - s.day) / 31.0


def months_per_round(start, end, rounds: int = 6) -> int:
    """⌈months to resolve_on / rounds⌉, at least 1."""
    return max(1, math.ceil(months_between(start, end) / rounds - 1e-9))


def compute_periods(start, end, rounds: int = 6) -> list[dict]:
    """Split [start, end] (inclusive) into `rounds` contiguous periods of near-equal length.
    Round 1 starts on `start`, the last round ends on `end` (resolve_on)."""
    s, e = _d(start), _d(end)
    total = (e - s).days + 1
    if total < rounds:
        raise ValueError(f"need at least {rounds} days between {s} and {e}")
    out = []
    for i in range(rounds):
        a = s + timedelta(days=(i * total) // rounds)
        b = s + timedelta(days=((i + 1) * total) // rounds - 1)
        out.append({"round": i + 1, "start": a.isoformat(), "end": b.isoformat()})
    return out


def period_of(data: dict, r: int) -> dict | None:
    periods = data.get("periods") or []
    for p in periods:
        if isinstance(p, dict) and p.get("round") == r:
            return p
    if 0 < r <= len(periods) and isinstance(periods[r - 1], dict):
        return periods[r - 1]
    return None


def period_line(data: dict, r: int) -> str:
    p = period_of(data, r)
    return f"本轮是 {p['start']}–{p['end']}。" if p and p.get("start") and p.get("end") else ""


def resolve_on(data: dict) -> str | None:
    periods = data.get("periods") or []
    return periods[-1].get("end") if periods and isinstance(periods[-1], dict) else None


# ---------- seed ----------
def seed_from_id(s: str) -> int:
    """First 4 bytes of sha1(id), big-endian. Stable across processes, unlike hash()."""
    return int.from_bytes(hashlib.sha1(s.encode("utf-8")).digest()[:4], "big")
