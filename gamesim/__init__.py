"""gamesim: LLM 博弈论推演框架。引擎只用标准库。

Importable (`from gamesim import ...`) and runnable as scripts
(`python -m gamesim.turn ...` or `python gamesim/turn.py ...`). The `gamesim` console script is gamesim.cli.

Engine API
  turn:       cmd_init, cmd_prep, cmd_gm, cmd_roll, cmd_apply, cmd_rebuild, committed_rounds, StepError, MoveError
  ingest:     ingest_round, ingest_setup, write_fallback_move, fallback_move, IngestError
  conditions: coverage_check, required_paths, patch_within, apply_patch, pointer_get, sort_conditions
  schema:     validate_entities, validate_events, validate_result_summary, finalize_assembled
  aggregate:  compute_k, markdown, digest, wilson
  target:     eval_target, run_hit, compute_periods, months_per_round, seed_from_id, deadline_text, render_conds
  templates:  render (assemble.md, conditions.md, result_summary.md, utility_research.md)
  fsio:       jdump (atomic), atomic_write_text, jload, extract_json, safe_path, ensure_inside, check_codename, PathError
Orchestration (need the `claude` CLI, or the fake one for tests)
  runner:     multi-run driver (claude -p per role, retries, fallbacks, branch swap)
  fork:       copy a run up to round N, inject events, continue (stress tests)
  strategy:   layer 2, score decision makers' strategy arms on layer-1 trajectories; sweep
"""
__version__ = "0.8.0"

from .aggregate import compute_k, digest, markdown, wilson  # noqa: E402,F401
from .conditions import (apply_patch, coverage_check, patch_within, pointer_get, required_paths,  # noqa: E402,F401
                         sort_conditions)
from .fsio import (PathError, atomic_write_text, check_codename, ensure_inside, extract_json, jdump,  # noqa: E402,F401
                   jload, safe_path)
from .ingest import IngestError, fallback_move, ingest_round, ingest_setup, write_fallback_move  # noqa: E402,F401
from .schema import (finalize_assembled, validate_entities, validate_events,  # noqa: E402,F401
                     validate_result_summary)
from .target import (compute_periods, deadline_text, eval_target, months_per_round, render_conds,  # noqa: E402,F401
                     run_hit, seed_from_id)
from .templates import render  # noqa: E402,F401
from .turn import (MoveError, StepError, cmd_apply, cmd_gm, cmd_init, cmd_prep, cmd_rebuild,  # noqa: E402,F401
                   cmd_roll, committed_rounds)
