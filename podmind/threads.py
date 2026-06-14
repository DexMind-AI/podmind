"""Thread library — surface narrative through-lines across a window of episodes.

Pivoted from `canvas.py` 2026-05-14 after the visual graph proved chaotic.
The insight that survived: **degree-≥2 cross-cutting entities are the
signal**; degree-1 entities are noise. Threads = LLM-named clusters
seeded by those bridges.

This module is pure-data (no LLM, no I/O outside reading wiki files).
LLM synthesis lives in `threads_llm.py`. Email/digest plumbing stays
in `bin/daily_digest.py`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from podmind import paths
from podmind.frontmatter import EpisodePage, parse_file

# Same patterns as canvas.py (the only reason these aren't shared is that
# canvas.py is being deleted — this is now their canonical home).
LINK_RE = {
    "people": re.compile(r"\[\[people/([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"),
    "topics": re.compile(r"\[\[topics/([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"),
}


def extract_links(episode_path: Path) -> tuple[set[str], set[str]]:
    """Return (people_slugs, topic_slugs) referenced from the episode body.

    Handles `[[people/slug]]`, `[[people/slug|Alias]]`, and
    `[[people/slug#section]]`. Drops placeholder slugs like `...`.
    """
    _, body = parse_file(episode_path)
    people = {m.group(1).strip() for m in LINK_RE["people"].finditer(body)}
    topics = {m.group(1).strip() for m in LINK_RE["topics"].finditer(body)}
    people = {s for s in people if s and s != "..." and "<" not in s}
    topics = {s for s in topics if s and s != "..." and "<" not in s}
    return people, topics


def existing_targets(kind: str) -> set[str]:
    """Slugs that have a real wiki page (vs dangling links)."""
    return {p.stem for p in (paths.WIKI_DIR / kind).glob("*.md")}


def bridges_in_window(
    episode_paths: Iterable[Path],
    *,
    min_degree: int = 2,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Find cross-cutting people/topics in this window.

    Returns ({person_slug: [ep_slug, ...]}, {topic_slug: [ep_slug, ...]}).
    Only entities referenced by ≥ min_degree episodes are included.
    Dangling references (no wiki page) are filtered out.

    These bridges are the seeds the LLM uses to identify threads — they
    capture the structural answer to "what connects what." The LLM may
    name a thread after a bridge, or it may invent a narrative title
    spanning multiple bridges.
    """
    people_targets = existing_targets("people")
    topic_targets = existing_targets("topics")

    raw_person: dict[str, list[str]] = defaultdict(list)
    raw_topic: dict[str, list[str]] = defaultdict(list)
    for path in episode_paths:
        ep_slug = path.stem
        people, topics = extract_links(path)
        for ps in people:
            if ps in people_targets:
                raw_person[ps].append(ep_slug)
        for t in topics:
            if t in topic_targets:
                raw_topic[t].append(ep_slug)

    person_bridges = {ps: eps for ps, eps in raw_person.items()
                      if len(eps) >= min_degree}
    topic_bridges = {t: eps for t, eps in raw_topic.items()
                     if len(eps) >= min_degree}
    return person_bridges, topic_bridges


# ---------- Data shapes for LLM output ----------

@dataclass
class Thread:
    """One narrative cluster across multiple episodes.

    The LLM produces these; we render them to markdown.
    """
    name: str                  # short narrative title, e.g. "Merz coalition crisis"
    summary: str               # one sentence (≤30 words)
    episode_slugs: list[str]   # wiki page stems

    def __post_init__(self):
        # Light validation — caller already filters but assert invariants
        # so a malformed LLM response surfaces early instead of in markdown.
        if not self.name.strip():
            raise ValueError("Thread.name is empty")
        if not self.episode_slugs:
            raise ValueError(f"Thread {self.name!r} has no episodes")


# ---------- Markdown rendering ----------

def _badge_for(ep: EpisodePage, duration_sec: int = 0) -> str:
    """Listened-state badge: 🎧 listened, ▶ N% in progress, ⚪ unplayed.

    Matches the convention in CLAUDE.md and `refresh_badges.badge_for`.
    Duration is optional — if present and we have played_up_to, compute %.
    """
    if ep.listened:
        return "🎧"
    if ep.played_up_to and ep.played_up_to > 0:
        if duration_sec and ep.played_up_to >= 0.95 * duration_sec:
            return "🎧"
        if duration_sec:
            return f"▶ {int(ep.played_up_to / duration_sec * 100)}%"
        return "▶"
    return "⚪"


def format_threads_md(
    threads: list[Thread],
    uncategorized: list[str],
    *,
    date_str: str,
    window_label: str = "the last 24h",
    episode_lookup: dict[str, EpisodePage] | None = None,
) -> str:
    """Render threads + uncategorized as a markdown digest body.

    `episode_lookup` maps slug → EpisodePage so we can put listened-state
    badges next to each episode bullet. Missing entries get a ⚪ default
    (better than crashing on a slug the LLM hallucinated).
    """
    lookup = episode_lookup or {}
    out: list[str] = []
    n_eps = sum(len(t.episode_slugs) for t in threads) + len(uncategorized)
    out.append(f"# Threads — {date_str}\n")
    out.append(f"_{n_eps} episodes across {len(threads)} threads in {window_label}._\n")

    for t in sorted(threads, key=lambda th: -len(th.episode_slugs)):
        out.append(f"## {t.name} ({len(t.episode_slugs)} episodes)\n")
        out.append(f"{t.summary}\n")
        for slug in t.episode_slugs:
            ep = lookup.get(slug)
            badge = _badge_for(ep) if ep else "⚪"
            hook_snippet = ""
            if ep and ep.hook:
                # Strip leading badge + show-link from the hook so the
                # bullet is just the editorial sentence.
                m = re.match(r"^[🎧▶⚪][^—]*—\s*(.*)", ep.hook)
                hook_snippet = (m.group(1) if m else ep.hook).strip()
                if len(hook_snippet) > 120:
                    hook_snippet = hook_snippet[:117] + "..."
            line = f"- [[episodes/{slug}]] {badge}"
            if hook_snippet:
                line += f" — {hook_snippet}"
            out.append(line)
        out.append("")  # blank line between threads

    if uncategorized:
        out.append(f"## Uncategorized ({len(uncategorized)})\n")
        out.append("_Episodes that didn't fit into a multi-episode thread._\n")
        for slug in uncategorized:
            ep = lookup.get(slug)
            badge = _badge_for(ep) if ep else "⚪"
            out.append(f"- [[episodes/{slug}]] {badge}")
        out.append("")

    return "\n".join(out)
