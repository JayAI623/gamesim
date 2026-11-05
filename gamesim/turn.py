#!/usr/bin/env python3
"""Round driver for gamesim.

  turn.py init    <run> [--from ENTITIES] [--seed-id ID]   # run.json + state.json + private/<code>.json
  turn.py prep    <run> <r> [--io file|stdout] [--events FILE]
                                                            # dice + user_events.json + turns/rNN/prompt_<code>.md
  turn.py gm      <run> <r> [--io file|stdout]              # validate moves; legacy: prompt_gm.md,
                                                            #   blind_dice: prompt_gm_commit.md (GM commits p_gm first)
  turn.py roll    <run> <r> [--io file|stdout]              # blind_dice only: roll vs committed p_gm, prompt_gm.md
  turn.py apply   <run> <r> [--attempt N]                   # round -> state -> private -> turns/rNN/commit.json
  turn.py rebuild <run>                                     # rebuild state.json + private/* from committed rounds

Add --json to print the result as one JSON object. Exit code 2 on invalid input or wrong step order.

Model outputs: turns/rNN/move_<code>.json, [gm_commit.json], gm.json
  --io file   : the prompt tells the model to Write its JSON to that path (agent with file tools).
  --io stdout : the prompt tells the model to print exactly one ```json block and use no tools; the caller ingests
                stdout with gamesim.ingest into the same file names.

A round is committed iff turns/rNN/commit.json exists. apply is idempotent; an uncommitted round is redone from
the committed rounds (state.json and private/* are rebuilt in memory, never trusted).

entities.json settings flags (default off): blind_dice, base_rate_cap, reference_class, discount, models.
v0.8 fields: target {conds, gm_flags, kind, min_rounds}, periods [{round, start, end}], agreement {name, parties, context}.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import dice as dice_mod
    from . import schema
    from . import target as tg
    from . import utility as ut
    from .fsio import (PathError, atomic_write_bytes, atomic_write_text, check_codename, ensure_inside, jdump,
                       jload, safe_path, sha1_file)
    from .templates import fill, template_body
except ImportError:  # run as a script: python gamesim/turn.py
    import dice as dice_mod
    import schema
    import target as tg
    import utility as ut
    from fsio import (PathError, atomic_write_bytes, atomic_write_text, check_codename, ensure_inside, jdump,
                      jload, safe_path, sha1_file)
    from templates import fill, template_body

MOVE_KEYS = ["options_considered", "chosen_index", "chosen", "position_now"]
GM_KEYS = ["state_vars", "public_state", "last_round_public"]
IO_MODES = ("file", "stdout")
LEGACY_DICE_RULE = ("对每个行动，先根据各方的实际能力和对手的行动，**独立判断**成功概率 p_gm"
                    "（不要照搬行动方自己估的 p）；骰子 r < p_gm 就判成功。互相冲突的行动要一起裁决。")
DELTA = {"short": 0.8, "long": 0.95}


class StepError(RuntimeError):
    """Command called out of order (round already committed, previous round not committed, run ended...)."""


class MoveError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class SchemaError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---------- paths ----------
def tdir(run, r) -> Path:
    return safe_path(run, "turns", f"r{r:02d}")


def turn_file(run, r, name) -> Path:
    return safe_path(run, "turns", f"r{r:02d}", name)


def round_file(run, r) -> Path:
    return safe_path(run, "rounds", f"round_{r:02d}.json")


def private_path(run, e) -> Path:
    return safe_path(run, "private", f"{check_codename(e['codename'])}.json")


def move_path(run, r, code) -> Path:
    return turn_file(run, r, f"move_{check_codename(code)}.json")


def read_root(run) -> Path:
    """Inputs (--from, --events) must live in the run dir or its parent (the gamesim run dir holding run_k/)."""
    return Path(run).resolve().parent


# ---------- run data ----------
def load_run(run):
    data = jload(safe_path(run, "entities.json"))
    codes = set()
    for e in data["entities"]:
        code = check_codename(e.get("codename"))
        if code in codes:
            raise PathError(f"duplicate codename {code}")
        codes.add(code)
    variables = {v["sym"]: v for v in data["variables"]}
    return data, variables


def run_meta(run) -> dict | None:
    p = safe_path(run, "run.json")
    return jload(p) if p.exists() else None


def run_seed(run, data) -> int:
    meta = run_meta(run)
    if meta and meta.get("seed") is not None:
        return int(meta["seed"])
    return int(data["settings"]["seed"])


def flag(data, key):
    return bool(data.get("settings", {}).get(key, False))


def delta_of(e):
    return e.get("delta", DELTA.get(e.get("time_preference", "long"), 0.9))


def agreement_of(data) -> dict | None:
    """Optional negotiated-agreement frame. Pre-v0.8 `ceasefire_context` maps to an agreement named 停火."""
    ag = data.get("agreement")
    if isinstance(ag, dict) and ag.get("name"):
        return {"name": ag["name"], "parties": ag.get("parties", ""), "context": ag.get("context", "")}
    if data.get("ceasefire_context"):
        return {"name": "停火", "parties": "", "context": data["ceasefire_context"]}
    return None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- prompt pieces ----------
def tool_rule(io):
    if io == "stdout":
        return "**规则：不要调用任何工具（不读文件、不搜索、不执行命令）。**"
    return "**规则：除了读取本 prompt 文件、写入你的输出文件，不要调用任何工具（不读其他文件、不搜索、不执行命令）。**"


def footer(io, out_path):
    if io == "stdout":
        return ("\n## 提交方式\n只输出一个 JSON 代码块（以 ```json 开头、``` 结尾），内容是上面要求的 JSON。"
                "不要调用任何工具；代码块前后不要写任何其他文字。\n")
    return (f"\n## 提交方式\n用 Write 工具把上面要求的 JSON（纯 JSON，不要代码块标记）写入：\n`{out_path}`\n"
            "写完后只回复一行：done。\n")


def agreement_block(ag):
    if not ag:
        return ""
    return f"\n## {ag['name']}条件（本次推演的起点）\n{ag['context']}\n"


def render_events_role(events):
    if not events:
        return ""
    lines = "\n".join(f"- {ev['text']}（来源: 用户）" for ev in events)
    return f"\n## 本轮新消息（来源: 用户）\n以下是本轮开始前已经发生的事实，各方都已知道：\n{lines}\n"


def render_events_gm(events):
    if not events:
        return ""
    lines = "\n".join(f"- [{ev['id']}] {ev['text']}（来源: 用户）" for ev in events)
    return ("\n## 已发生事件（来源: 用户，不掷骰）\n以下事件已经真实发生，是给定的事实：不要给它们概率，不要掷骰，"
            "也不要把它们列为外生事件；直接把它们的后果纳入本轮裁决和状态变量。\n" + lines + "\n")


def entity_extra_rules(data, e) -> list[str]:
    rules = []
    if flag(data, "base_rate_cap"):
        rules.append("**基准率上限**：如果你选的行动偏离了你自己的历史基准率（比如基准率显示你通常会报复/搅局/不让步，"
                     "而你这次不这样做，或者反过来），那么该行动的 p_success 不得高于对应基准率，"
                     "并且必须在 `deviation_reason` 里写出本轮出现的、过去没有的具体新事实。")
    if flag(data, "discount"):
        rules.append(f"**折现**：你的月折现因子 δ = {delta_of(e)}。收益如果要在 k 个月后才兑现，要先乘以 δ^k，"
                     "再写进 payoff_success / payoff_fail，并在 `why` 里注明 k。")
    return rules


def entity_tasks_tail(data, e):
    items = []
    ag = agreement_of(data)
    if ag:
        items.append(f"写下你**本轮能接受的{ag['name']}底线**（`agreement_reservation`）：最少要得到什么、最多能给出什么、"
                     "在先后顺序上的底线（谁先动）。")
    items.append("可选：给其他参与方发私下消息（由 GM 下一轮转交）。")
    items += entity_extra_rules(data, e)
    return "".join(f"\n{5 + i}. {t}" for i, t in enumerate(items))


def gm_extra_rules(data, phase):
    rules = []
    ag = agreement_of(data)
    if flag(data, "base_rate_cap") and phase in ("legacy", "commit"):
        rules.append("对偏离行动方自身历史基准率的行动，p_gm 不得高于该基准率，除非本轮出现了具体的新事实（要写明）。")
    if flag(data, "reference_class") and phase in ("legacy", "resolve"):
        rules.append(data.get("reference_class_rule") or
                     "**先写参照类**：给出 p_target 之前，先在 `reference_class` 里写出参照类（类似局面的历史样本）"
                     "及其基准率（例如 a/b），再说明本轮具体证据把它往上或往下调了多少；"
                     "同时给出 90% 区间 `p_target_ci90`（形如 [0.1, 0.4]）。")
    if data.get("exogenous_priors") and phase in ("legacy", "commit"):
        rules.append("**背景方先验**：外生事件里凡是能对上「背景方先验」key 的，概率不得超过它的上沿；超过时在 why 里写出新事实。"
                     "汇报结果时只用结果词典（outcomes.md）里的 key 和定义。")
    if phase == "resolve":
        verdict = f"裁决{ag['name']}、" if ag else ""
        rules.append("**成败已定**：各行动和外生事件的成败已由脚本按你先前承诺的概率掷骰决定（见骰子部分），不得更改；"
                     f"你只负责描述后果、{verdict}更新状态变量。")
    return "".join(f"\n- {r}" for r in rules)


def render_state(state_vars, variables):
    return "；".join(f"{s}={state_vars[s]:g}（{variables[s]['name']}）" for s in variables)


def render_card(e, variables, state_vars, show_delta=False):
    terms = " + ".join(f"{t['weight']:.2f}·u_{t['var']}" for t in e["utility"])
    rows = ["| 变量 | 含义 | 当前值 | 权重 | 方向/形状 | 说明 |", "|---|---|---|---|---|---|"]
    for t in e["utility"]:
        v = variables[t["var"]]
        shape = t.get("shape", "linear")
        if t.get("threshold") is not None:
            shape += f"@{t['threshold']}"
        rows.append(f"| {t['var']} | {v['name']} | {state_vars[t['var']]:g} | {t['weight']:.2f} | "
                    f"{t.get('direction', '+')} {shape} | {t.get('note', '')} |")
    out = [f"U = {terms}（每个 u_k ∈ [−1,+1]；方向 + 表示越大越好；peak@x 表示 x 处最好的倒 U）", *rows]
    if e.get("red_lines"):
        out.append("红线（硬约束，被突破则 U = −10）：" +
                   "；".join(f"{r['rule']}（{r['var']} {r['op']} {r['threshold']}）" for r in e["red_lines"]))
    if e.get("soft_rules"):
        out.append("行为规则：" + "；".join(e["soft_rules"]))
    out.append(f"风险态度 {e.get('risk')}（−1 厌恶 … +1 偏好）；时间偏好 {e.get('time_preference')}"
               + (f"；月折现因子 δ = {delta_of(e)}" if show_delta else ""))
    if e.get("capabilities"):
        out.append("能力：" + e["capabilities"])
    if e.get("internal_split"):
        out.append("内部分歧：" + e["internal_split"])
    u = ut.utility(e, variables, state_vars)
    out.append(f"按当前状态，你的 U = {u['U']:+.2f}")
    return "\n".join(out)


def render_exogenous(data):
    """Exogenous events, plus rule A: priors borrowed from runs where the background actor was a full player."""
    out = [f"- {x}" for x in data.get("exogenous", [])]
    pri = data.get("exogenous_priors") or {}
    if pri:
        out.append("\n**背景方先验（规则 A：来自把这一方当独立玩家的其他推演局，你给这些事件的概率不得超过上沿，"
                   "除非写出本轮出现的、来源局里没有的新事实）**")
        for k, v in pri.items():
            rates = "；".join(f"{rk}={rv}" for rk, rv in v.items() if rk.startswith("base_rate"))
            out.append(f"- `{k}`：{rates}（{v.get('n', '')}；来源 {v.get('source_run', '')}）{v.get('note', '')}")
    return "\n".join(out) or "（无）"


def render_base_rates(e):
    return "\n".join(f"- {b['pattern']}：{b['rate']}" for b in e.get("base_rates", [])) or "（无）"


def render_menu(data):
    return "\n".join(f"- {a['level']}：{a['action']}" for a in data["action_menu"])


def render_axis(data):
    return f"0 = {data['issue']['low']}；100 = {data['issue']['high']}"


def render_private(p):
    out = [f"【初始私有信息】{p['private_info']}"]
    for lg in p["log"]:
        seg = [f"【第 {lg['round']} 轮】你的日记：{lg.get('diary', '')}"]
        if lg.get("private_update"):
            seg.append(f"你得知：{lg['private_update']}")
        out.append(" ".join(seg))
    inbox = p["log"][-1].get("inbox", []) if p["log"] else []
    inbox_txt = "\n".join(f"- 来自 {m.get('from', '?')}：{m.get('content', '')}" for m in inbox
                          if isinstance(m, dict)) or "（无）"
    return "\n".join(out), inbox_txt


# ---------- state machine: pure functions ----------
def initial_state(data, variables):
    return {"round": 0, "public_state": data["initial_public_state"],
            "last_round_public": "（推演开始，尚无上一轮）",
            "state_vars": {s: v["current"] for s, v in variables.items()}, "history": []}


def initial_privates(data):
    return {e["codename"]: {"name": e["name"], "private_info": e["private_info"], "log": []} for e in data["entities"]}


def normalize_gm(gm: dict) -> dict:
    """Pre-v0.8 GM outputs: ceasefire -> agreement, p_recovery_* -> p_target*."""
    if "agreement" not in gm and isinstance(gm.get("ceasefire"), dict):
        gm["agreement"] = gm["ceasefire"]
    for old, new in (("p_recovery_6m", "p_target"), ("p_recovery_ci90", "p_target_ci90"),
                     ("p_recovery_reason", "p_target_reason")):
        if new not in gm and old in gm:
            gm[new] = gm[old]
    return gm


def advance(state, privates, data, variables, record):
    """Apply one committed round record {round, moves, gm, user_events?} to (state, privates). Pure."""
    state, privates = copy.deepcopy(state), copy.deepcopy(privates)
    r, gm, moves = record["round"], normalize_gm(dict(record["gm"])), record["moves"]
    sv = gm["state_vars"]
    hit = tg.eval_target(data.get("target"), sv, gm)
    state.update({"round": r, "public_state": gm["public_state"],
                  "last_round_public": gm["last_round_public"], "state_vars": sv})
    state["history"].append({
        "round": r, "state_vars": sv, "agreement": gm.get("agreement") or {},
        "p_target": gm.get("p_target"), "p_target_ci90": gm.get("p_target_ci90"),
        "hit": hit, "recovered": hit,  # `recovered` = pre-v0.8 alias
        "p_outcomes": gm.get("p_outcomes"), "terminal": tg.truthy(gm.get("terminal", False)),
        "events_used": [ev["id"] for ev in record.get("user_events") or []]})
    pu, dm = gm.get("private_updates", {}) or {}, gm.get("delivered_messages", {}) or {}
    for e in data["entities"]:
        mv = moves.get(e["name"], {})
        privates[e["codename"]]["log"].append({"round": r, "diary": mv.get("diary", ""),
                                               "private_update": pu.get(e["name"], ""),
                                               "inbox": dm.get(e["name"], [])})
    return state, privates


def committed_rounds(run) -> list[int]:
    """Contiguous committed rounds 1..c. Pre-v0.8 run dirs (no run.json, no commit.json) trust state.json."""
    out, r = [], 1
    while turn_file(run, r, "commit.json").exists():
        out.append(r)
        r += 1
    if out or safe_path(run, "run.json").exists():
        return out
    sp = safe_path(run, "state.json")
    if not sp.exists():
        return []
    n = int(jload(sp).get("round", 0))
    legacy = []
    for k in range(1, n + 1):
        if not round_file(run, k).exists():
            break
        legacy.append(k)
    return legacy


def is_legacy(run) -> bool:
    return not safe_path(run, "run.json").exists() and not turn_file(run, 1, "commit.json").exists()


def rebuild(run, data, variables, upto: int):
    """state + privates after committed rounds 1..upto, replayed from rounds/round_NN.json (sha1-checked)."""
    state, privates = initial_state(data, variables), initial_privates(data)
    for k in range(1, upto + 1):
        rp, cp = round_file(run, k), turn_file(run, k, "commit.json")
        if cp.exists():
            want = (jload(cp).get("sha1") or {}).get(f"rounds/round_{k:02d}.json")
            if want and sha1_file(rp) != want:
                raise StepError(f"rounds/round_{k:02d}.json does not match the sha1 in its commit.json")
        state, privates = advance(state, privates, data, variables, jload(rp))
    return state, privates


def write_state(run, data, state, privates) -> dict:
    sha = {"state.json": jdump(state, safe_path(run, "state.json"))}
    for e in data["entities"]:
        sha[f"private/{e['codename']}.json"] = jdump(privates[e["codename"]], private_path(run, e))
    return sha


def ensure_state(run, data, variables):
    """state.json consistent with the committed rounds; rebuilt from rounds/ if not."""
    c = len(committed_rounds(run))
    sp = safe_path(run, "state.json")
    try:
        state = jload(sp)
        ok = state.get("round") == c and all(
            len(jload(private_path(run, e)).get("log", [])) == c for e in data["entities"])
    except (OSError, ValueError, KeyError):
        ok = False
    if ok:
        return state
    if is_legacy(run):
        raise StepError("pre-v0.8 run dir with inconsistent state.json; cannot rebuild safely")
    state, privates = rebuild(run, data, variables, c)
    write_state(run, data, state, privates)
    return state


def terminal_round(run, committed) -> int | None:
    for k in committed:
        if tg.truthy((jload(round_file(run, k)).get("gm") or {}).get("terminal", False)):
            return k
    return None


def check_next(run, data, r):
    """Round r must be the next uncommitted round of a run that has not ended."""
    committed = committed_rounds(run)
    if r in committed:
        raise StepError(f"round {r} is already committed")
    if r != len(committed) + 1:
        raise StepError(f"round {r} is not next: committed rounds are {committed or 'none'}")
    if r > data["settings"]["rounds"]:
        raise StepError(f"round {r} > settings.rounds {data['settings']['rounds']}")
    tr = terminal_round(run, committed)
    if tr:
        raise StepError(f"run ended at round {tr} (terminal); later rounds are absorbed")
    return committed


# ---------- user events ----------
def used_event_ids(run, before_round) -> set[str]:
    used = set()
    for k in range(1, before_round):
        cp = turn_file(run, k, "commit.json")
        if cp.exists():
            used.update(jload(cp).get("events_used") or [])
    return used


def freeze_events(run, r, events_file=None) -> list[dict]:
    """Write turns/rNN/user_events.json from the caller's frozen batch; reuse it on re-prep."""
    p = turn_file(run, r, "user_events.json")
    if events_file is None:
        return jload(p).get("events", []) if p.exists() else []
    src = ensure_inside(read_root(run), events_file)
    raw = jload(src)
    evs = raw.get("events") if isinstance(raw, dict) else raw
    errs = schema.validate_events(evs)
    if errs:
        raise SchemaError(errs)
    used = used_event_ids(run, r)
    evs = sorted(({k: ev[k] for k in ("id", "seq", "text", "created_at") if k in ev}
                  for ev in evs if ev["id"] not in used), key=lambda ev: ev["seq"])
    if p.exists():
        old = [ev.get("id") for ev in jload(p).get("events", [])]
        moves_exist = any(tdir(run, r).glob("move_*.json"))
        if old != [ev["id"] for ev in evs] and moves_exist:
            raise StepError(f"user events of round {r} are frozen: moves were already produced with {old}")
    jdump({"round": r, "source": "用户", "events": evs}, p)
    return evs


# ---------- commands ----------
def cmd_init(run, from_entities=None, seed_id=None, force=False):
    Path(run).mkdir(parents=True, exist_ok=True)
    if from_entities:
        src = ensure_inside(read_root(run), from_entities)
        atomic_write_bytes(safe_path(run, "entities.json"), Path(src).read_bytes())
    data, variables = load_run(run)
    errs = schema.validate_entities(data, require_target=False, require_periods=False)
    if errs:
        raise SchemaError(errs)
    if committed_rounds(run) and not force:
        raise StepError("run already has committed rounds; use rebuild (or --force to start over)")
    if seed_id:
        seed = tg.seed_from_id(seed_id)
    elif data["settings"].get("seed") is not None:
        seed = int(data["settings"]["seed"])
    else:
        raise SchemaError(["no seed: pass --seed-id or set settings.seed"])
    jdump({"seed": seed, "seed_id": seed_id, "created_at": now_iso()}, safe_path(run, "run.json"))
    write_state(run, data, initial_state(data, variables), initial_privates(data))
    return {"entities": len(data["entities"]), "seed": seed}


def cmd_rebuild(run):
    data, variables = load_run(run)
    c = len(committed_rounds(run))
    state, privates = rebuild(run, data, variables, c)
    write_state(run, data, state, privates)
    return {"round": c}


def cmd_prep(run, r, io="file", events_file=None):
    data, variables = load_run(run)
    check_next(run, data, r)
    state = ensure_state(run, data, variables)
    names = [e["name"] for e in data["entities"]]
    dice = dice_mod.draws(run_seed(run, data), r, names + ["_exo"])
    jdump(dice, turn_file(run, r, "dice.json"))
    events = freeze_events(run, r, events_file)
    tpl = template_body("entity.md", run)
    tpr = data["settings"]["time_per_round"]
    out = []
    for e in data["entities"]:
        notes, inbox = render_private(jload(private_path(run, e)))
        others = "、".join(n for n in names if n != e["name"])
        out_path = move_path(run, r, e["codename"])
        syms = list(variables)[:2]
        body = fill(tpl, {
            "name": e["name"], "tool_rule": tool_rule(io),
            "payoff_card": render_card(e, variables, state["state_vars"], show_delta=flag(data, "discount")),
            "state_vars": render_state(state["state_vars"], variables),
            "base_rates": render_base_rates(e), "private_notes": notes, "round": r,
            "max_rounds": data["settings"]["rounds"], "period_line": tg.period_line(data, r),
            "public_state": state["public_state"], "agreement_block": agreement_block(agreement_of(data)),
            "last_round_public": state["last_round_public"], "user_events_block": render_events_role(events),
            "inbox": inbox, "action_menu": render_menu(data), "issue_axis": render_axis(data),
            "tasks_tail": entity_tasks_tail(data, e),
            "state_change_example": json.dumps(dict(zip(syms, ["+5", "-10"])), ensure_ascii=False),
            "agreement_json_line": ('\n  "agreement_reservation": {"must_get": "…", "can_give": "…", "sequencing": "…"},'
                                    if agreement_of(data) else "")})
        header = (f"# 第 {r} 轮 · 你扮演：{e['name']}（{e.get('members', '')}）\n"
                  f"其他参与方：{others}。每轮代表 {tpr}。\n\n")
        p = turn_file(run, r, f"prompt_{e['codename']}.md")
        atomic_write_text(p, header + body + footer(io, out_path))
        model = (data.get("settings", {}).get("models") or {}).get(e["name"], "")
        out.append({"code": e["codename"], "name": e["name"], "prompt": str(p), "out": str(out_path), "model": model})
    return {"round": r, "io": io, "events": [ev["id"] for ev in events], "roles": out}


def validate_move(m):
    if not isinstance(m, dict):
        return ["not a JSON object"]
    missing = [k for k in MOVE_KEYS if k not in m]
    if "chosen" in m and (not isinstance(m["chosen"], dict) or "escalation" not in m["chosen"]):
        missing.append("chosen.escalation")
    return missing


def load_moves(run, r, data):
    moves, bad = {}, []
    for e in data["entities"]:
        p = move_path(run, r, e["codename"])
        if not p.exists():
            bad.append(f"{e['codename']} {e['name']}: missing file")
            continue
        try:
            m = jload(p)
        except Exception as ex:  # noqa: BLE001
            bad.append(f"{e['codename']} {e['name']}: bad json ({ex})")
            continue
        miss = validate_move(m)
        if miss:
            bad.append(f"{e['codename']} {e['name']}: missing keys {miss}")
        moves[e["name"]] = m
    if bad:
        raise MoveError(bad)
    return moves


def write_gm_prompt(run, r, data, variables, state, moves, phase, io="file", dice=None, outcomes_txt="",
                    events=()):
    cards = []
    for e in data["entities"]:
        cards.append(f"### {e['name']}（{e.get('members', '')}，影响力 {e['weight']}）\n"
                     f"{render_card(e, variables, state['state_vars'], show_delta=flag(data, 'discount'))}\n"
                     f"私有信息：{e['private_info']}")
    base_all = "\n".join(f"**{e['name']}**\n{render_base_rates(e)}" for e in data["entities"])
    dice_section = (json.dumps(dice or {}, ensure_ascii=False) + "\n" + LEGACY_DICE_RULE) if phase == "legacy" \
        else outcomes_txt
    ag = agreement_of(data)
    target = data.get("target") or {}
    flags = target.get("gm_flags") or []
    rounds = data["settings"]["rounds"]
    mapping = {
        "tool_rule": tool_rule(io),
        "entities_full": "\n\n".join(cards), "round": r, "max_rounds": rounds,
        "period_line": tg.period_line(data, r), "public_state": state["public_state"],
        "agreement_block": agreement_block(ag), "events_block": render_events_gm(events),
        "reservation_note": f"和{ag['name']}底线" if ag else "",
        "moves": "\n\n".join(f"**{n}**\n```json\n{json.dumps(m, ensure_ascii=False, indent=1)}\n```"
                             for n, m in moves.items()),
        "state_vars": render_state(state["state_vars"], variables),
        "variables": "；".join(f"{s}: {v['name']} [{v['lo']}, {v['hi']}]" for s, v in variables.items()),
        "base_rates_all": base_all, "exogenous": render_exogenous(data),
        "names": "、".join(e["name"] for e in data["entities"]),
        "dice_section": dice_section, "extra_rules": gm_extra_rules(data, phase),
        "agreement_rule": (f"\n- **{ag['name']}裁决**：对照各方的 `agreement_reservation`，判断"
                           f"{ag['parties'] or '关键各方'}之间是否存在谈判区间（各方都能接受的条件交集）。"
                           f"{ag['name']}只有在关键方都实际行动支持时才算生效；任何一方（包括搅局者）的行动都可能让它破裂。"
                           f"裁决结果写进 `agreement`，`in_effect` 表示本轮末{ag['name']}是否有效。") if ag else "",
        "target_def": tg.render_conds(target, variables) if target else "（本推演未定义目标状态）",
        "deadline_text": tg.deadline_text(target.get("kind", "ever"), target.get("min_rounds"),
                                          tg.resolve_on(data), rounds),
        "flags_rule": ("\n- 你的输出 JSON 必须给出这些布尔字段（true 或 false）："
                       + "、".join(f"`{f}`" for f in flags) + "。") if flags else "",
        "state_example": json.dumps({s: state["state_vars"][s] for s in variables}, ensure_ascii=False),
        "agreement_json_line": ('\n  "agreement": {"in_effect": false, "terms": "≤80字，若无写空", '
                                '"zopa": "≤80字：各方底线是否有交集、差在哪", "spoilers": "≤40字"},') if ag else ""}
    if phase == "commit":
        body = fill(template_body("gm_commit.md", run), mapping)
        out_path, p = turn_file(run, r, "gm_commit.json"), turn_file(run, r, "prompt_gm_commit.md")
        title = "Game Master（第一阶段：先承诺概率，看不到骰子）"
    else:
        body = fill(template_body("game_master.md", run), mapping)
        out_path, p, title = turn_file(run, r, "gm.json"), turn_file(run, r, "prompt_gm.md"), "Game Master"
    atomic_write_text(p, f"# 第 {r} 轮 · {title}\n\n" + body + footer(io, out_path))
    return p, out_path


def cmd_gm(run, r, io="file"):
    data, variables = load_run(run)
    check_next(run, data, r)
    state = ensure_state(run, data, variables)
    moves = load_moves(run, r, data)
    jdump(moves, turn_file(run, r, "_moves.json"))
    events = freeze_events(run, r)
    if flag(data, "blind_dice"):
        p, o = write_gm_prompt(run, r, data, variables, state, moves, "commit", io, events=events)
        phase = "commit"
    else:
        p, o = write_gm_prompt(run, r, data, variables, state, moves, "legacy", io,
                               dice=jload(turn_file(run, r, "dice.json")), events=events)
        phase = "legacy"
    return {"round": r, "phase": phase, "prompt": str(p), "out": str(o)}


def cmd_roll(run, r, io="file"):
    """Blind dice: compare committed p_gm with pre-drawn dice, then write the resolve prompt."""
    data, variables = load_run(run)
    check_next(run, data, r)
    state = ensure_state(run, data, variables)
    commit = jload(turn_file(run, r, "gm_commit.json"))
    dice = jload(turn_file(run, r, "dice.json"))
    moves = jload(turn_file(run, r, "_moves.json"))
    seed = run_seed(run, data)
    outcomes = {"actions": {}, "exogenous": []}
    probs = commit.get("probs") or {}
    for e in data["entities"]:
        c = probs.get(e["name"], {})
        p = float(c.get("p_gm", 0.5))
        outcomes["actions"][e["name"]] = {
            "p_gm": p, "dice": dice[e["name"]], "succeeded": dice[e["name"]] < p,
            "action": c.get("action") or moves[e["name"]]["chosen"].get("action", "")}
    for i, x in enumerate(commit.get("exogenous") or []):
        rr = dice["_exo"] if i == 0 else dice_mod.draws(seed * 7 + i, r, ["x"])["x"]
        pe = float(x.get("p", 0))
        outcomes["exogenous"].append({"event": x.get("event", ""), "p": pe, "dice": rr, "happened": rr < pe})
    jdump(outcomes, turn_file(run, r, "outcomes.json"))
    lines = ["（盲骰：以下 p_gm 是你在看到骰子之前承诺的；成败已由脚本判定，不得更改）"]
    for n, o in outcomes["actions"].items():
        lines.append(f"- {n}：{o['action'][:80]} → p_gm {o['p_gm']}，骰 {o['dice']}，"
                     f"{'成功' if o['succeeded'] else '失败'}")
    for x in outcomes["exogenous"]:
        lines.append(f"- 外生：{x['event']} → p {x['p']}，骰 {x['dice']}，{'发生' if x['happened'] else '未发生'}")
    if commit.get("reference_class"):
        lines.append("你在第一阶段写的参照类：" + json.dumps(commit["reference_class"], ensure_ascii=False))
    p, o = write_gm_prompt(run, r, data, variables, state, moves, "resolve", io, outcomes_txt="\n".join(lines),
                           events=freeze_events(run, r))
    return {"round": r, "phase": "resolve", "prompt": str(p), "out": str(o), "outcomes": outcomes}


def cmd_apply(run, r, attempt=0):
    """Commit round r: write rounds/round_NN.json, state.json, private/*, then turns/rNN/commit.json."""
    cp = turn_file(run, r, "commit.json")
    if cp.exists():
        return {**jload(cp), "already_committed": True}
    data, variables = load_run(run)
    committed = check_next(run, data, r)
    state, privates = rebuild(run, data, variables, len(committed))
    gm = jload(turn_file(run, r, "gm.json"))
    if not isinstance(gm, dict) or [k for k in GM_KEYS if k not in gm]:
        raise MoveError([f"gm.json missing {[k for k in GM_KEYS if k not in gm] if isinstance(gm, dict) else GM_KEYS}"])
    gm = normalize_gm(gm)
    oc_path = turn_file(run, r, "outcomes.json")
    if flag(data, "blind_dice") and oc_path.exists():
        oc = jload(oc_path)
        gm["blind_outcomes"] = oc
        for res in gm.get("resolution", []) or []:
            o = oc["actions"].get(res.get("entity")) if isinstance(res, dict) else None
            if o:
                res.update({"p_gm": o["p_gm"], "dice": o["dice"], "succeeded": o["succeeded"]})
        gm["exogenous_events"] = oc["exogenous"]
    sv = dict(state["state_vars"])
    for s, v in (gm["state_vars"] or {}).items():
        if s in variables:
            try:
                sv[s] = max(variables[s]["lo"], min(variables[s]["hi"], float(v)))
            except (TypeError, ValueError):
                pass
    gm["state_vars"] = sv
    moves = load_moves(run, r, data)
    events = freeze_events(run, r)
    record = {"round": r, "moves": moves, "gm": gm}
    if events:
        record["user_events"] = events
    sha = {f"rounds/round_{r:02d}.json": jdump(record, round_file(run, r))}       # 1. round
    state, privates = advance(state, privates, data, variables, record)
    sha.update(write_state(run, data, state, privates))                            # 2. state, 3. private/*
    commit = {"round": r, "attempt": attempt, "events_used": [ev["id"] for ev in events],
              "sha1": sha, "committed_at": now_iso()}
    jdump(commit, cp)                                                              # 4. commit.json
    h = state["history"][-1]
    return {**commit, "already_committed": False, "hit": h["hit"], "terminal": h["terminal"],
            "summary": apply_summary(data, variables, record, h)}


def apply_summary(data, variables, record, h):
    r, gm, moves = record["round"], record["gm"], record["moves"]
    sv = gm["state_vars"]
    esc = " ".join(f"{e['name']}{moves[e['name']]['chosen'].get('escalation')}" for e in data["entities"])
    us = " ".join(f"{e['name']}{ut.utility(e, variables, sv)['U']:+.2f}" for e in data["entities"])
    ag = gm.get("agreement") or {}
    lines = [f"R{r} | 升级 {esc}", "   | 状态 " + " ".join(f"{s}={sv[s]:g}" for s in variables), f"   | U {us}",
             f"   | 协议 {ag.get('in_effect')} | GM p_target {gm.get('p_target')} {gm.get('p_target_ci90') or ''} "
             f"| hit {h['hit']} | terminal {h['terminal']}"]
    if gm.get("p_outcomes"):
        lines.append("   | 结果概率 " + json.dumps(gm["p_outcomes"], ensure_ascii=False)[:300])
    if gm.get("reference_class"):
        lines.append(f"   | 参照类 {json.dumps(gm['reference_class'], ensure_ascii=False)[:200]}")
    lines.append(f"   | {gm['last_round_public']}")
    return lines


# ---------- CLI ----------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["init", "prep", "gm", "roll", "apply", "rebuild"])
    ap.add_argument("run")
    ap.add_argument("round", nargs="?", type=int, default=0)
    ap.add_argument("--io", choices=IO_MODES, default="file")
    ap.add_argument("--events", help="frozen user-event batch for this round (prep only)")
    ap.add_argument("--attempt", type=int, default=0)
    ap.add_argument("--from", dest="from_entities", help="init: copy this entities.json into the run dir")
    ap.add_argument("--seed-id", help="init: seed = first 4 bytes of sha1(SEED_ID)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    run = os.path.expanduser(a.run)
    if a.cmd in ("prep", "gm", "roll", "apply") and a.round < 1:
        ap.error(f"{a.cmd} needs a round number >= 1")
    try:
        if a.cmd == "init":
            res = cmd_init(run, a.from_entities, a.seed_id, a.force)
            text = [f"init ok: {res['entities']} entities, seed {res['seed']}"]
        elif a.cmd == "rebuild":
            res = cmd_rebuild(run)
            text = [f"rebuild ok: round {res['round']}"]
        elif a.cmd == "prep":
            res = cmd_prep(run, a.round, a.io, a.events)
            text = [f"{o['code']}\t{o['name']}\t{o['prompt']}\t{o['out']}\t{o['model']}" for o in res["roles"]]
        elif a.cmd in ("gm", "roll"):
            res = (cmd_gm if a.cmd == "gm" else cmd_roll)(run, a.round, a.io)
            text = [f"{res['prompt']}\t{res['out']}"]
            for n, oc in (res.get("outcomes") or {}).get("actions", {}).items():
                text.append(f"  {n}: p={oc['p_gm']} dice={oc['dice']} -> {'OK' if oc['succeeded'] else 'FAIL'}")
        else:
            res = cmd_apply(run, a.round, a.attempt)
            text = [f"R{a.round} already committed"] if res["already_committed"] else res["summary"]
    except MoveError as ex:
        print("INVALID\n" + "\n".join(ex.problems))
        return 2
    except (StepError, PathError, SchemaError, ValueError, KeyError, FileNotFoundError) as ex:
        print(f"ERROR {type(ex).__name__}: {ex}", file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False) if a.json else "\n".join(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
