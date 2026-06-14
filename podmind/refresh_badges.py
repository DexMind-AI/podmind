"""Refresh listened-state badges on existing wiki episode pages.

For every wiki/episodes/*.md, find the corresponding raw/episodes/*/meta.json,
recompute the badge from current `listened` / `played_up_to` / `duration_sec`,
and update both the YAML frontmatter and the first body line. No other edits.

Idempotent. Safe to run repeatedly.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

from podmind import paths

console = Console()


def badge_for(meta: dict) -> str:
    listened = bool(meta.get("listened"))
    played = meta.get("played_up_to") or 0
    duration = meta.get("duration_sec") or 0
    if listened or (duration > 0 and played >= 0.95 * duration):
        return "🎧"
    if played > 0:
        pct = int(played / duration * 100) if duration else 0
        return f"▶ {pct}%"
    return "⚪"


def find_meta_for_wiki_episode(wiki_path: Path) -> Path | None:
    """Resolve raw meta.json via the `raw_dir:` frontmatter key.

    All wiki episode pages carry `raw_dir: <show-slug>/<episode-slug>` set at
    ingest time (or via the one-shot backfill). Without it we'd match the
    wrong dir for episodes that exist in both PC (`<show>/...`) and YouTube
    (`yt-<channel>/...`) variants.
    """
    text = wiki_path.read_text()
    fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return None
    rd_match = re.search(r"^raw_dir:\s*(\S+)\s*$", fm_match.group(1), re.M)
    if not rd_match:
        return None
    candidate = paths.EPISODES_DIR / rd_match.group(1) / "meta.json"
    return candidate if candidate.exists() else None


FM_BADGE_KEYS = ("listened", "played_up_to")
BADGE_LINE_RE = re.compile(r"^(🎧|▶ \d+%|⚪)\s+(.*)$")


def update_episode_file(wiki_path: Path, meta_path: Path) -> tuple[str, str] | None:
    """Returns (old_badge, new_badge) if changed, None if no change."""
    meta = json.loads(meta_path.read_text())
    new_badge = badge_for(meta)
    text = wiki_path.read_text()

    # Parse frontmatter and body
    fm_match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not fm_match:
        return None
    fm, body = fm_match.group(1), fm_match.group(2)

    # Update YAML frontmatter listened/played_up_to fields
    new_fm = fm
    new_fm = re.sub(r"^listened:.*$", f"listened: {str(meta.get('listened', False)).lower()}", new_fm, flags=re.M)
    new_fm = re.sub(r"^played_up_to:.*$", f"played_up_to: {meta.get('played_up_to', 0)}", new_fm, flags=re.M)

    # Update first non-blank body line if it starts with a badge
    new_body_lines = body.split("\n")
    old_badge = ""
    for i, line in enumerate(new_body_lines):
        if not line.strip():
            continue
        bm = BADGE_LINE_RE.match(line)
        if bm:
            old_badge = bm.group(1)
            new_body_lines[i] = f"{new_badge} {bm.group(2)}"
        break

    new_text = f"---\n{new_fm}\n---\n" + "\n".join(new_body_lines)
    if new_text == text:
        return None
    wiki_path.write_text(new_text)
    return (old_badge or "?", new_badge)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description=(
            "Re-derive listened-state badges on all wiki episode pages from "
            "the current meta.json values. Idempotent — safe to run repeatedly. "
            "No arguments change behavior; --help is the only flag."
        )
    )
    ap.parse_args(argv)

    wiki_eps = sorted((paths.WIKI_DIR / "episodes").glob("*.md"))
    wiki_eps = [p for p in wiki_eps if p.stem != "_template"]
    console.print(f"[bold]{len(wiki_eps)}[/] wiki episode pages")

    changed = unchanged = nometa = 0
    changes: list[tuple[str, str, str]] = []
    for wp in wiki_eps:
        mp = find_meta_for_wiki_episode(wp)
        if not mp:
            nometa += 1
            continue
        result = update_episode_file(wp, mp)
        if result:
            changed += 1
            changes.append((wp.name, *result))
        else:
            unchanged += 1

    console.print(f"changed={changed}  unchanged={unchanged}  no-meta={nometa}")
    for name, old, new in changes[:50]:
        console.print(f"  {old:6} → {new:6}  {name}")

    if changed:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with paths.LOG_FILE.open("a") as f:
            f.write(f"\n## [{ts}] refresh-badges\n- changed: {changed} episode pages\n- unchanged: {unchanged}\n")
            if nometa:
                f.write(f"- no-meta-match: {nometa} (orphan wiki pages?)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
