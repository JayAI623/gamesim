"""Prompt templates: load prompts/<name>, strip the doc header, fill {{placeholders}} in one pass.

Callers render the setup prompts with `render("assemble.md", {...})` etc.; turn.py renders the round prompts.
A run dir may override a template with <run>/prompts/<name> or <run>/../prompts/<name>.
"""
from __future__ import annotations

import os
import re

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_PH_RE = re.compile(r"\{\{(\w+)\}\}")


def template_path(name: str, run: str | None = None) -> str:
    if run:
        run = os.path.abspath(run)
        for base in (run, os.path.dirname(run)):
            p = os.path.join(base, "prompts", name)
            if os.path.isfile(p) and not os.path.islink(p):
                return p
    return os.path.join(PROMPTS_DIR, name)


def template_body(name: str, run: str | None = None) -> str:
    """Template text without its leading header block ('# title' line, '>' doc lines, blank lines)."""
    with open(template_path(name, run), encoding="utf-8") as f:
        lines = f.read().splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith(">") or
                              (i == 0 and lines[i].startswith("# "))):
        i += 1
    return "\n".join(lines[i:]).strip() + "\n"


def placeholders(tpl: str) -> set[str]:
    return set(_PH_RE.findall(tpl))


def fill(tpl: str, mapping: dict) -> str:
    """Single-pass substitution, so substituted content containing '{{x}}' is left alone.
    Raises KeyError naming every placeholder missing from mapping."""
    missing = placeholders(tpl) - set(mapping)
    if missing:
        raise KeyError(f"template placeholders not filled: {sorted(missing)}")
    return _PH_RE.sub(lambda m: str(mapping[m.group(1)]), tpl)


def render(name: str, mapping: dict, run: str | None = None) -> str:
    return fill(template_body(name, run), mapping)
