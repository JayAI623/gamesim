"""Point GAMESIM_CLAUDE_BIN at the bundled fake claude for a test (no model calls, no network)."""
import os
from contextlib import contextmanager
from unittest import mock

from gamesim import runner


@contextmanager
def fake_claude(refuse: str = "", log_path: str | None = None):
    env = {"GAMESIM_CLAUDE_BIN": runner.fake_claude_cmd(), "GAMESIM_FAKE_REFUSE": refuse}
    if log_path:
        env["GAMESIM_FAKE_LOG"] = log_path
    with mock.patch.dict(os.environ, env):
        yield


def calls(log_path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not os.path.exists(log_path):
        return out
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            k = line.split("\t", 1)[0]
            out[k] = out.get(k, 0) + 1
    return out
