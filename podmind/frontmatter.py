"""YAML-frontmatter parser for wiki pages.

The wiki uses Obsidian-style markdown with a YAML block at the top:

    ---
    show: The Breakdown
    date: 2026-04-29
    listened: true
    played_up_to: 1200
    guests: [Andy Stumpf]
    ---

    🎧 [[shows/the-breakdown]] — body starts here...

Five different parsers grew up around this format (line-by-line scans in
`bin/_lib`, `bin/tick_prep`; ad-hoc regexes in `bin/build_stats`,
`bin/tick_finalize`, `podmind/refresh_badges`). All slightly different,
all subtly fragile. This module is the single source of truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_FM_PATTERN = re.compile(r"\A---\n(.*?\n)?---\n?(.*)\Z", re.S)


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body).

    Returns ({}, original_text) if no frontmatter is present. Returns
    ({}, body) on YAML parse errors so callers can still get the body.
    Empty frontmatter blocks return ({}, body).
    """
    m = _FM_PATTERN.match(text)
    if not m:
        return {}, text
    raw_yaml = m.group(1) or ""  # group(1) is None for empty frontmatter
    body = m.group(2).lstrip("\n")
    if not raw_yaml.strip():
        return {}, body
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return {}, body
    if not isinstance(data, dict):
        return {}, body
    return data, body


def parse_file(path: Path) -> tuple[dict[str, Any], str]:
    """Read `path` and parse its frontmatter. Errors propagate as OSError."""
    return parse(path.read_text(errors="ignore"))


def _safe_int(val: Any) -> int:
    """Coerce a frontmatter value to int, tolerating `~15`, `null`, `'12 min'`, etc."""
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        m = re.search(r"-?\d+", val)
        return int(m.group(0)) if m else 0
    return 0


def first_body_line(body: str) -> str:
    """The first non-empty line of the body — typically the listened-state badge
    line followed by the hook (`🎧 [[shows/foo]] — Bob discusses ...`)."""
    for line in body.splitlines():
        if line.strip():
            return line
    return ""


@dataclass(frozen=True)
class EpisodePage:
    """Typed view of a wiki episode page's frontmatter + body summary.

    Fields mirror the schema documented in CLAUDE-vault.md. Missing fields
    fall back to safe defaults; check `raw_dir` for presence (it's the only
    truly required field for the pipeline).
    """
    raw_dir: str | None
    date: str | None
    show: str | None
    listened: bool
    played_up_to: int
    duration_min: int
    guests: list[str]
    transcript_source: str | None
    body: str
    hook: str  # first non-empty body line

    @classmethod
    def from_text(cls, text: str) -> "EpisodePage":
        fm, body = parse(text)
        return cls(
            raw_dir=fm.get("raw_dir"),
            date=str(fm["date"]) if "date" in fm and fm["date"] is not None else None,
            show=fm.get("show"),
            listened=bool(fm.get("listened", False)),
            played_up_to=_safe_int(fm.get("played_up_to")),
            duration_min=_safe_int(fm.get("duration_min")),
            guests=list(fm.get("guests") or []),
            transcript_source=fm.get("transcript_source"),
            body=body,
            hook=first_body_line(body),
        )

    @classmethod
    def from_file(cls, path: Path) -> "EpisodePage":
        return cls.from_text(path.read_text(errors="ignore"))


# Convenience functions for the common cases. These replace the line-by-line
# scans in `bin/_lib.parse_raw_dir`, `bin/tick_prep`, and the ad-hoc regexes
# in `bin/build_stats` and `bin/tick_finalize`.

def read_raw_dir(path: Path) -> str | None:
    """Return the `raw_dir` field from a wiki episode page, or None if absent."""
    fm, _ = parse_file(path)
    val = fm.get("raw_dir")
    return str(val) if val is not None else None


def read_date(path: Path) -> str | None:
    """Return the `date` field as a string (YYYY-MM-DD), or None if absent or
    unparseable."""
    fm, _ = parse_file(path)
    val = fm.get("date")
    return str(val) if val is not None else None
