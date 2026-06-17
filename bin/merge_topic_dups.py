#!/usr/bin/env python
"""Find and apply duplicate topic-page merge candidates."""
from __future__ import annotations

import argparse
import itertools
import re
from pathlib import Path

from _lib import WIKI_DIR
from podmind.proposals import parse_file


def _tokens(slug: str) -> set[str]:
    return {t for t in re.split(r"[-_]+", slug.lower()) if t}


def _similarity(a: str, b: str) -> float:
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _topic_slugs() -> list[tuple[str, Path]]:
    topics_dir = WIKI_DIR / "topics"
    if not topics_dir.exists():
        return []
    return sorted((p.stem, p) for p in topics_dir.glob("*.md"))


def find_pairs(threshold: float = 0.6, recent: int | None = None) -> list[tuple[str, str, float]]:
    """Return likely duplicate topic slug pairs.

    When recent is set, only pairs touching one of the most recently modified
    topic files are returned. That keeps nightly curation bounded while still
    comparing recent pages against the whole topic set.
    """
    topics = _topic_slugs()
    recent_slugs: set[str] | None = None
    if recent is not None:
        newest = sorted(topics, key=lambda item: item[1].stat().st_mtime, reverse=True)
        recent_slugs = {slug for slug, _ in newest[:recent]}

    out: list[tuple[str, str, float]] = []
    for (a, _), (b, _) in itertools.combinations(topics, 2):
        if recent_slugs is not None and a not in recent_slugs and b not in recent_slugs:
            continue
        score = _similarity(a, b)
        if score >= threshold:
            out.append((a, b, score))
    return out


def _apply_from(path: Path) -> int:
    # Parsing validates the approved checklist format. The actual destructive
    # vault rewrite is deliberately not implemented in this compatibility layer.
    parse_file(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find duplicate topic pages")
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--recent", type=int, default=None)
    ap.add_argument("--apply-from", type=Path)
    args = ap.parse_args(argv)

    if args.apply_from:
        return _apply_from(args.apply_from)
    for a, b, score in find_pairs(threshold=args.threshold, recent=args.recent):
        print(f"{score:.3f}\t{a}\t{b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
