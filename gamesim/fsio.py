"""File IO shared by the engine: atomic writes, tolerant JSON reads, path safety.

- Every write goes to a temp file in the same directory, is fsync'ed, then renamed over the target.
- Every path the engine writes is built with `safe_path(root, ...)` or checked with `ensure_inside(root, path)`:
  resolved, must stay inside the run dir, and no component below the run dir may be a symlink.
- Codenames are spliced into file names, so they must match CODENAME_RE.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

CODENAME_RE = re.compile(r"^[A-Z][a-z]{2,15}$")


class PathError(ValueError):
    """A path or codename that would escape the run dir, or goes through a symlink."""


# ---------- path safety ----------
def check_codename(code) -> str:
    if not isinstance(code, str) or not CODENAME_RE.fullmatch(code):
        raise PathError(f"bad codename {code!r}: must match ^[A-Z][a-z]{{2,15}}$")
    return code


def ensure_inside(root, path) -> Path:
    """Return `path` as an absolute Path if it stays inside `root` without passing through a symlink.

    The root itself may be reached through a symlink (macOS /tmp -> /private/tmp); nothing below it may.
    """
    root_r = Path(root).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if ".." in p.parts:
        raise PathError(f"path {str(path)!r} contains '..'")
    below = []
    cur = p
    while True:
        if (cur.exists() or cur.is_symlink()) and cur.resolve() == root_r:
            break
        below.append(cur)
        if cur.parent == cur:
            raise PathError(f"path {str(path)!r} is outside run dir {str(root_r)!r}")
        cur = cur.parent
    for c in below:
        if c.is_symlink():
            raise PathError(f"path {str(path)!r} goes through symlink {str(c)!r}")
    if not p.resolve().is_relative_to(root_r):
        raise PathError(f"path {str(path)!r} is outside run dir {str(root_r)!r}")
    return p


def safe_path(root, *parts) -> Path:
    """root/part/... after checking each part (no '..', no absolute parts, no empty parts) and `ensure_inside`."""
    for part in parts:
        s = str(part)
        if not s or os.path.isabs(s) or any(seg in ("..", "") for seg in s.split("/")):
            raise PathError(f"bad path component {s!r}")
    return ensure_inside(root, Path(root).joinpath(*map(str, parts)))


# ---------- atomic writes ----------
def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path, data: bytes) -> str:
    """tmp + fsync + rename. Returns the sha1 hex of `data`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path.parent)
    return hashlib.sha1(data).hexdigest()


def atomic_write_text(path, text: str) -> str:
    return atomic_write_bytes(path, text.encode("utf-8"))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def jdump(obj, path) -> str:
    """Atomic JSON write. Returns sha1 hex of the bytes written."""
    return atomic_write_text(path, dumps(obj))


def sha1_file(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


# ---------- tolerant reads ----------
_FENCE_RE = re.compile(r"```([A-Za-z]*)[ \t]*\n?(.*?)```", re.S)


def extract_json(text: str):
    """Parse model output: bare JSON, a ```json fenced block, any fenced block, or the first JSON value in prose."""
    if not isinstance(text, str):
        raise ValueError("expected text")
    s = text.strip().lstrip("﻿")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fences = _FENCE_RE.findall(s)
    for lang, body in sorted(fences, key=lambda fb: fb[0].lower() != "json"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            continue
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    for start in sorted(starts):
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
            return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON value found in text")


def jload(path):
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return extract_json(txt)
