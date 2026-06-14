"""Atomic JSON file writes.

meta.json files are read-modify-written by daily.sh (sync/cascade),
whisper.sh (cascade), tick_prep (dup quarantine), tick_finalize (corrupt
quarantine), and manual scripts — sometimes concurrently. A plain
write_text interrupted mid-flush leaves truncated JSON; every reader
catches JSONDecodeError and skips, so the episode is silently orphaned
forever. tmp + rename is atomic on the same filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, obj: Any, *, indent: int = 2) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent))
    tmp.rename(path)
