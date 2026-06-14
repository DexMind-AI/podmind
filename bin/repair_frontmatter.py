#!/usr/bin/env -S uv run python
"""repair_frontmatter — fix episode pages whose YAML frontmatter fails to parse.

Root cause (2026-06-11): tick_finalize wrote unquoted scalar values, so any
show name with a colon ("Real Vision: Finance & Investing") produced invalid
YAML. parse() returned {} → read_raw_dir → None → tick_prep re-dispatched the
episode every day forever. 992 pages affected.

This repairs them WITHOUT re-summarizing: the body is intact; only the
frontmatter block needs valid quoting. We line-edit the raw frontmatter
(regex per known key) rather than round-tripping through the broken parser.

Usage:
    ./bin/repair_frontmatter.py --dry-run    # report only
    ./bin/repair_frontmatter.py              # apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from podmind import paths
from podmind.frontmatter import parse_file

FM_RE = re.compile(r"\A(---\n)(.*?\n)(---\n?)(.*)\Z", re.S)
# Keys whose values are free-text scalars that may contain YAML-breaking chars.
SCALAR_KEYS = ("show", "transcript_source", "raw_dir")


def _yaml_str(s: str) -> str:
    escaped = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _already_quoted(val: str) -> bool:
    v = val.strip()
    return len(v) >= 2 and v[0] == '"' and v[-1] == '"'


def repair_text(text: str) -> str | None:
    """Return repaired file text, or None if no change needed/possible."""
    m = FM_RE.match(text)
    if not m:
        return None
    head, fm_block, tail_sep, body = m.groups()
    out_lines = []
    changed = False
    for line in fm_block.splitlines():
        # match `key: value` for our scalar keys
        km = re.match(r"^(\w+):[ \t]*(.*)$", line)
        if km and km.group(1) in SCALAR_KEYS:
            key, val = km.group(1), km.group(2)
            if val and not _already_quoted(val):
                line = f"{key}: {_yaml_str(val)}"
                changed = True
        out_lines.append(line)
    if not changed:
        return None
    return head + "\n".join(out_lines) + "\n" + tail_sep + body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eps = sorted((paths.WIKI_DIR / "episodes").glob("*.md"))
    broken_before = repaired = still_broken = 0
    for p in eps:
        fm, _ = parse_file(p)
        if fm and fm.get("raw_dir"):
            continue  # parses fine
        broken_before += 1
        new_text = repair_text(p.read_text(errors="ignore"))
        if new_text is None:
            still_broken += 1
            continue
        if not args.dry_run:
            p.write_text(new_text)
        # verify the repair parses
        from podmind.frontmatter import parse
        fm2, _ = parse(new_text)
        if fm2.get("raw_dir"):
            repaired += 1
        else:
            still_broken += 1
            if args.dry_run:
                print(f"  [would-still-fail] {p.name}")

    verb = "would repair" if args.dry_run else "repaired"
    print(f"broken frontmatter pages: {broken_before}")
    print(f"{verb}: {repaired}")
    print(f"unrepairable (needs manual look): {still_broken}")


if __name__ == "__main__":
    main()
