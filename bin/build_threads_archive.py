#!/usr/bin/env -S uv run python
"""build_threads_archive — backfill monthly thread pages.

For each month since project start, generate `wiki/threads/by-month/<YYYY-MM>.md`:
a thread-clustered view of every episode the user listened to in that month.
Opens in Obsidian; "rewind the year" is just opening months in sequence.

Usage:
    ./bin/build_threads_archive.py --month 2026-01    # one month
    ./bin/build_threads_archive.py --all              # all months from earliest data
    ./bin/build_threads_archive.py --all --since 2025-08  # from a starting month
    ./bin/build_threads_archive.py --month 2026-05 --dry-run

Episode selection per month:
- Walk wiki/episodes/*.md, keep those where date YYYY-MM matches AND
  the user engaged (listened=true OR played_up_to > 0).
- Cap at 80 episodes/month to stay within the configured LLM provider's context window.
  Selection priority: listened-complete > in-progress > just-started,
  then most-recent first.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from podmind import paths
from podmind.frontmatter import EpisodePage
from podmind.threads import bridges_in_window, format_threads_md
from podmind.threads_llm import synthesize_threads


MAX_EPISODES_PER_MONTH = 80
ARCHIVE_DIR = paths.WIKI_DIR / "threads" / "by-month"


def episodes_in_month(year_month: str) -> list[tuple[Path, EpisodePage]]:
    """Return wiki episode pages where date startswith year_month and the
    user engaged (listened or played_up_to > 0). Sorted by engagement
    then date, capped at MAX_EPISODES_PER_MONTH."""
    candidates: list[tuple[Path, EpisodePage]] = []
    for p in (paths.WIKI_DIR / "episodes").glob("*.md"):
        try:
            ep = EpisodePage.from_file(p)
        except OSError:
            continue
        if not ep.date or not ep.date.startswith(year_month):
            continue
        engaged = ep.listened or (ep.played_up_to and ep.played_up_to > 0)
        if not engaged:
            continue
        candidates.append((p, ep))

    candidates.sort(key=lambda kv: (kv[1].listened == False,
                                     not (kv[1].played_up_to and kv[1].played_up_to > 600),
                                     not (kv[1].played_up_to and kv[1].played_up_to > 0),
                                     -datetime.strptime(kv[1].date, "%Y-%m-%d").toordinal()
                                       if kv[1].date else 0))
    return candidates[:MAX_EPISODES_PER_MONTH]


def discover_months_with_listens() -> list[str]:
    """Scan wiki/episodes and return ordered YYYY-MM strings for every month
    that has at least one listened/played episode."""
    seen: set[str] = set()
    for p in (paths.WIKI_DIR / "episodes").glob("*.md"):
        try:
            ep = EpisodePage.from_file(p)
        except OSError:
            continue
        if not ep.date:
            continue
        engaged = ep.listened or (ep.played_up_to and ep.played_up_to > 0)
        if engaged:
            seen.add(ep.date[:7])
    return sorted(seen)


def build_month(year_month: str, *, dry_run: bool = False) -> Path | None:
    pairs = episodes_in_month(year_month)
    if len(pairs) < 2:
        print(f"  {year_month}: skipped ({len(pairs)} engaged episodes — need ≥2)",
              file=sys.stderr)
        return None
    print(f"  {year_month}: {len(pairs)} episodes", file=sys.stderr)

    person_br, topic_br = bridges_in_window([p for p, _ in pairs])
    print(f"    bridges: {len(person_br)} people, {len(topic_br)} topics",
          file=sys.stderr)

    eps_with_slugs = [(p.stem, ep) for p, ep in pairs]
    print(f"    calling configured LLM provider...", file=sys.stderr)
    threads, uncat = synthesize_threads(
        eps_with_slugs, person_br, topic_br,
    )
    print(f"    → {len(threads)} threads + {len(uncat)} uncategorized",
          file=sys.stderr)

    lookup = {p.stem: ep for p, ep in pairs}
    md = format_threads_md(
        threads, uncat,
        date_str=year_month,
        window_label=f"{year_month}",
        episode_lookup=lookup,
    )
    # Patch the header to be month-shaped rather than date-shaped
    md = md.replace(f"# Threads — {year_month}\n",
                    f"# Threads — {year_month}\n")  # already month-shaped via date_str

    if dry_run:
        print(f"    [dry-run] would write {len(md)} chars", file=sys.stderr)
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE_DIR / f"{year_month}.md"
    out.write_text(md)
    print(f"    → wrote {out}", file=sys.stderr)
    return out


def regenerate_index() -> Path:
    """Write wiki/threads/by-month/index.md listing every monthly archive."""
    months = sorted(p.stem for p in ARCHIVE_DIR.glob("*.md") if p.stem != "index")
    lines = ["# Threads archive\n",
             "_Monthly retrospective of listening, clustered by theme. Open in sequence to rewind the year._\n"]
    by_year: dict[str, list[str]] = defaultdict(list)
    for m in months:
        by_year[m[:4]].append(m)
    for year in sorted(by_year, reverse=True):
        lines.append(f"\n## {year}\n")
        for m in sorted(by_year[year], reverse=True):
            lines.append(f"- [[threads/by-month/{m}]]")
    out = ARCHIVE_DIR / "index.md"
    out.write_text("\n".join(lines) + "\n")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", help="single month, YYYY-MM")
    g.add_argument("--all", action="store_true", help="every month with ≥2 engaged eps")
    ap.add_argument("--since", help="only months ≥ this YYYY-MM (with --all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.month:
        months = [args.month]
    else:
        months = discover_months_with_listens()
        if args.since:
            months = [m for m in months if m >= args.since]
        print(f"discovered {len(months)} months: {months[0]}..{months[-1]}", file=sys.stderr)

    for m in months:
        build_month(m, dry_run=args.dry_run)

    if not args.dry_run:
        idx = regenerate_index()
        print(f"→ index: {idx}", file=sys.stderr)


if __name__ == "__main__":
    main()
