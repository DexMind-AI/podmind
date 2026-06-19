"""Combine the digest's two episode-source passes into the final selection.

`daily_digest` collects episodes from two passes — the PocketCasts listening
history (recency-ordered, exposes real progress) and a date/watched-at window
over the wiki (which surfaces YouTube watches and anything PocketCasts missed).
This module unions them: dedupe by raw_dir (PocketCasts wins, preserving its
pca.st link), sort by page date descending, cap to a maximum.

Pure — no I/O — so it is unit-tested directly, independent of the un-importable
bin/daily_digest.py script. See
docs/superpowers/specs/2026-06-17-youtube-in-digest-design.md.
"""
from __future__ import annotations

from podmind.frontmatter import EpisodePage


def merge_episode_sources(
    primary: list[EpisodePage],
    secondary: list[EpisodePage],
    *,
    max_n: int,
) -> tuple[list[EpisodePage], int]:
    """Union `primary` (PocketCasts) with `secondary` (window pass, incl. yt-*).

    Dedupe by `raw_dir` — an entry in `primary` wins over a `secondary` entry
    with the same `raw_dir`, so PocketCasts metadata/links are preserved.
    Episodes with a falsy `raw_dir` carry no dedupe key and are always kept.
    Sort the union by page `date` descending (missing dates sort last), then
    cap to `max_n`.

    Returns `(kept, dropped)` where `dropped = max(0, total - max_n)`.
    """
    kept = list(primary)
    seen = {e.raw_dir for e in primary if e.raw_dir}
    for e in secondary:
        if e.raw_dir and e.raw_dir in seen:
            continue
        kept.append(e)
        if e.raw_dir:
            seen.add(e.raw_dir)
    kept.sort(key=lambda e: e.date or "", reverse=True)
    dropped = max(0, len(kept) - max_n)
    return kept[:max_n], dropped
