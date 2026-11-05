#!/usr/bin/env python3
"""A stand-in for `claude -p` (tests and dry runs). Standalone: imports nothing from gamesim.

Reads the prompt on stdin, tells what kind of prompt it is and prints one valid ```json block:
  role      (# 第 r 轮 · 你扮演：…)           -> a move, escalation 1–4
  gm_commit (第一阶段：先承诺概率)             -> probs for every party
  gm        (# 第 r 轮 · Game Master)         -> state_vars drifted ±4 from the current state, required flags
  scorer    (打分员)                           -> F components for every arm × round
Answers are deterministic (seeded by the prompt text), so a fake run is reproducible.

Environment:
  GAMESIM_FAKE_REFUSE  comma-separated party names whose role prompts get a refusal (no JSON) every time
  GAMESIM_FAKE_LOG     append one line "<kind>\\t<first 40 chars>" per call to this file

Use it:  GAMESIM_CLAUDE_BIN="python3 /path/to/gamesim/fake_claude.py" gamesim run …   (or `gamesim run --fake`)
"""
import hashlib
import json
import os
import random
import re
import sys


def kind(prompt: str) -> str:
    if "打分员" in prompt:
        return "scorer"
    if "第一阶段：先承诺概率" in prompt:
        return "gm_commit"
    if re.search(r"^# 第 \d+ 轮 · Game Master", prompt, re.M):
        return "gm"
    if "你扮演：" in prompt:
        return "role"
    return "other"


def block(obj) -> str:
    return "```json\n" + json.dumps(obj, ensure_ascii=False, indent=1) + "\n```"


def role(prompt, rng):
    name = re.search(r"你扮演：(.+?)（", prompt).group(1)
    if name in [x.strip() for x in os.environ.get("GAMESIM_FAKE_REFUSE", "").split(",") if x.strip()]:
        return "抱歉，我不能以真实政府的第一人称撰写军事行动方案。"
    esc = rng.randint(1, 4)
    return block({"entity": name, "beliefs": {},
                  "options_considered": [{"action": "维持并观察", "p_success": 0.6, "payoff_success": 2,
                                          "payoff_fail": -1, "eu": 0.8, "why": "假 claude"}],
                  "chosen_index": 0,
                  "chosen": {"action": f"{name}：第 {esc} 级行动", "target": "all", "escalation": esc,
                             "public_statement": "（假 claude）"},
                  "deviation_reason": "", "expected_state_change": {}, "position_now": rng.randint(20, 80),
                  "private_messages": [], "diary": "假 claude 的日记"})


def names_in(prompt):
    m = re.search(r"必须是：(.+?)>", prompt) or re.search(r"键必须用这些名字之一：(.+?)。", prompt)
    return [x for x in m.group(1).split("、") if x] if m else []


def gm_commit(prompt, rng):
    return block({"probs": {n: {"action": "谈判", "p_gm": round(rng.uniform(0.2, 0.8), 2), "why": "假"}
                            for n in names_in(prompt)},
                  "exogenous": [{"event": "外生冲击", "p": 0.2}]})


def gm(prompt, rng):
    r = int(re.search(r"^# 第 (\d+) 轮", prompt, re.M).group(1))
    m = re.search(r'"state_vars": (\{.*?\})', prompt, re.S)
    cur = json.loads(m.group(1)) if m else {}
    sv = {k: round(float(v) + rng.uniform(-4, 4), 1) for k, v in cur.items()}
    out = {"round": r, "resolution": [], "public_state": f"第 {r} 轮公开状态（假 claude）",
           "last_round_public": f"第 {r} 轮各方谈判（假 claude）", "private_updates": {}, "delivered_messages": {},
           "state_vars": sv, "state_reasons": {}, "p_target": round(rng.uniform(0.05, 0.5), 2),
           "p_target_ci90": [0.05, 0.6], "p_target_reason": "假", "terminal": False, "terminal_reason": "",
           "reference_class": {"class": "假参照类", "base_rate": "1/10"}}
    fm = re.search(r"必须给出这些布尔字段（true 或 false）：(.+?)。", prompt)
    for f in re.findall(r"`([A-Za-z_][\w.]*)`", fm.group(1)) if fm else []:
        cur_obj = out
        keys = f.split(".")
        for k in keys[:-1]:
            cur_obj = cur_obj.setdefault(k, {})
        cur_obj[keys[-1]] = rng.random() < 0.3
    return block(out)


def scorer(prompt, rng):
    dm = re.search(r"决策者：`([^`]+)`", prompt).group(1)
    arms = re.findall(r"`([^`]+)`", re.search(r"策略臂字段：(.+)", prompt).group(1))
    comps = re.findall(r"`([^`]+)`", re.search(r"分项字段：(.+)", prompt).group(1))
    labels = re.findall(r"`([^`]+)`", re.search(r"轮次标签：(.+)", prompt).group(1))
    out = {"dm": dm, "arms": {}, "notes": "假打分员"}
    for i, a in enumerate(arms):
        rows = []
        for k, lab in enumerate(labels, 1):
            row = {"round": k, "label": lab}
            for c in comps:
                row[c] = max(0, min(100, 90 - 8 * i + rng.randint(-5, 5)))
            row["why"] = "假"
            rows.append(row)
        out["arms"][a] = rows
    return block(out)


def main():
    argv = sys.argv[1:]
    assert "-p" in argv and "--no-session-persistence" in argv, argv
    assert argv[argv.index("--tools") + 1] == "", "tools must be empty"
    prompt = sys.stdin.read()
    k = kind(prompt)
    if os.environ.get("GAMESIM_FAKE_LOG"):
        with open(os.environ["GAMESIM_FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(f"{k}\t{prompt[:40].splitlines()[0]}\n")
    rng = random.Random(int(hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12], 16))
    fn = {"role": role, "gm_commit": gm_commit, "gm": gm, "scorer": scorer}.get(k)
    if fn is None:
        print("（假 claude：不认识这种 prompt）")
        return 1
    print(fn(prompt, rng))
    return 0


if __name__ == "__main__":
    sys.exit(main())
