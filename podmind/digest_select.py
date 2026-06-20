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

# A podcast must be meaningfully consumed to earn a place in the digest —
# mirrors the vault's "≥50% played = listened" policy. YouTube watches have no
# progress signal but are written `listened=true`, so they pass on that alone.
DIGEST_MIN_PLAYED_FRACTION = 0.5


def is_engaged(ep: EpisodePage) -> bool:
    """True if the episode was consumed enough to belong in the digest:
    marked `listened`, or played to ≥ DIGEST_MIN_PLAYED_FRACTION of its length.
    A few-seconds tap (a 2% play) is not engagement and is excluded."""
    if ep.listened:
        return True
    if ep.duration_min and ep.played_up_to:
        return ep.played_up_to >= DIGEST_MIN_PLAYED_FRACTION * ep.duration_min * 60
    return False


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
