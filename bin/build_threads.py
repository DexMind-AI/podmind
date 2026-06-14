#!/usr/bin/env -S uv run python
"""build_threads — preview the threaded digest format on a window of episodes.

Reuses daily_digest.collect_episodes for the "what did I listen to recently"
query. The output is what the daily digest body will look like after Phase 3.

Usage:
    ./bin/build_threads.py                          # last 24h, print to stdout
    ./bin/build_threads.py --hours 168              # last 7 days
    ./bin/build_threads.py --out /tmp/threads.md    # write to a file too
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from podmind import paths
from podmind.frontmatter import EpisodePage, read_raw_dir
from podmind.threads import bridges_in_window, format_threads_md
from podmind.threads_llm import synthesize_threads

import daily_digest  # for collect_episodes


def _ep_paths_and_pages(eps: list[EpisodePage]) -> list[tuple[Path, EpisodePage]]:
    """Map EpisodePages back to their wiki file paths via raw_dir."""
    by_raw_dir: dict[str, Path] = {}
    for p in (paths.WIKI_DIR / "episodes").glob("*.md"):
        rd = read_raw_dir(p)
        if rd:
            by_raw_dir[rd] = p
    out: list[tuple[Path, EpisodePage]] = []
    for ep in eps:
        if ep.raw_dir and ep.raw_dir in by_raw_dir:
            out.append((by_raw_dir[ep.raw_dir], ep))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--out", type=Path, help="also write to this path")
    args = ap.parse_args()

    print(f"collecting episodes from last {args.hours}h...", file=sys.stderr)
    eps = daily_digest.collect_episodes(hours=args.hours, max_n=80)
    if not eps:
        print(f"no episodes in window", file=sys.stderr)
        return

    pairs = _ep_paths_and_pages(eps)
    print(f"  {len(pairs)} episodes with wiki pages", file=sys.stderr)

    paths_only = [p for p, _ in pairs]
    person_br, topic_br = bridges_in_window(paths_only)
    print(f"  bridges: {len(person_br)} people, {len(topic_br)} topics",
          file=sys.stderr)

    eps_with_slugs = [(p.stem, ep) for p, ep in pairs]
    print(f"  calling configured LLM provider for thread synthesis...", file=sys.stderr)
    threads, uncat = synthesize_threads(
        eps_with_slugs, person_br, topic_br,
    )
    print(f"  → {len(threads)} threads + {len(uncat)} uncategorized",
          file=sys.stderr)

    date_str = datetime.now().strftime("%Y-%m-%d")
    window_label = f"the last {args.hours}h" if args.hours != 24 else "the last 24h"
    lookup = {p.stem: ep for p, ep in pairs}
    md = format_threads_md(threads, uncat, date_str=date_str,
                           window_label=window_label, episode_lookup=lookup)
    print(md)
    if args.out:
        args.out.write_text(md)
        print(f"\n→ wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
