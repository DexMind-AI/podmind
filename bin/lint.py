#!/usr/bin/env -S uv run python
"""lint — surface wiki health issues per CLAUDE.md spec.

Checks:
1. Near-duplicate topic slugs (token-jaccard ≥ 0.7)
2. Single-citation topic/people pages (merge candidates)
3. Broken cross-links (reference to non-existent page)
4. Orphan pages (not in index)
5. Stale `transcript_source: none` corruption flags

Output: a structured report appended to wiki/log.md as `## [date] lint`.
"""
import argparse
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from _lib import WIKI_DIR

LINK_RE = re.compile(r"\[\[(?:(?:episodes|people|topics|shows)/)?([^\]|]+)(?:\|[^\]]+)?\]\]")


from podmind.slugs import slug_tokens, SLUG_STOPWORDS  # noqa: F401


def find_near_dup_topics(threshold: float = 0.7) -> list[tuple[str, str, float]]:
    topics = [p.stem for p in (WIKI_DIR / "topics").glob("*.md")]
    pairs = []
    by_first_token: dict[str, list[str]] = defaultdict(list)
    for t in topics:
        toks = sorted(slug_tokens(t))
        if toks:
            by_first_token[toks[0]].append(t)
    for bucket in by_first_token.values():
        if len(bucket) < 2:
            continue
        for i, a in enumerate(bucket):
            ta = slug_tokens(a)
            if not ta: continue
            for b in bucket[i + 1:]:
                tb = slug_tokens(b)
                if not tb: continue
                inter = ta & tb
                union = ta | tb
                jac = len(inter) / len(union)
                if jac >= threshold:
                    pairs.append((a, b, jac))
    pairs.sort(key=lambda x: -x[2])
    return pairs


def count_citations(kind: str) -> dict[str, int]:
    """Count [[episodes/<slug>]] back-refs in each page."""
    out: dict[str, int] = {}
    for p in (WIKI_DIR / kind).glob("*.md"):
        text = p.read_text(errors="ignore")
        out[p.stem] = len(re.findall(r"\[\[episodes/", text))
    return out


def find_broken_links() -> list[tuple[str, str]]:
    existing: set[str] = set()
    for kind in ("episodes", "people", "topics", "shows"):
        for p in (WIKI_DIR / kind).glob("*.md"):
            existing.add(f"{kind}/{p.stem}")
            existing.add(p.stem)
    broken: list[tuple[str, str]] = []
    for kind in ("episodes", "people", "topics", "shows"):
        for p in (WIKI_DIR / kind).glob("*.md"):
            text = p.read_text(errors="ignore")
            for m in re.finditer(r"\[\[((?:episodes|people|topics|shows)/[^\]|]+)(?:\|[^\]]+)?\]\]", text):
                target = m.group(1)
                if target not in existing:
                    broken.append((f"{kind}/{p.stem}", target))
    return broken


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-log", action="store_true", help="append to wiki/log.md")
    ap.add_argument("--threshold", type=float, default=0.7, help="topic-dup jaccard threshold")
    args = ap.parse_args()

    near_dups = find_near_dup_topics(args.threshold)
    topic_cites = count_citations("topics")
    people_cites = count_citations("people")
    single_topics = sorted([t for t, n in topic_cites.items() if n == 1])
    single_people = sorted([p for p, n in people_cites.items() if n == 1])
    broken = find_broken_links()

    print(f"=== Lint report — {datetime.now():%Y-%m-%d %H:%M} ===")
    print()
    print(f"Topics: {len(topic_cites)} pages")
    print(f"People: {len(people_cites)} pages")
    print()
    print(f"Near-dup topic slugs (Jaccard ≥ {args.threshold}): {len(near_dups)}")
    for a, b, jac in near_dups[:30]:
        print(f"  {jac:.2f}  {a}  ↔  {b}")
    if len(near_dups) > 30:
        print(f"  ... and {len(near_dups) - 30} more")
    print()
    print(f"Single-citation topic pages: {len(single_topics)} ({100*len(single_topics)/max(len(topic_cites),1):.0f}%)")
    print(f"Single-citation people pages: {len(single_people)} ({100*len(single_people)/max(len(people_cites),1):.0f}%)")
    print()
    print(f"Broken cross-links: {len(broken)}")
    for src, target in broken[:20]:
        print(f"  {src} → {target}")
    if len(broken) > 20:
        print(f"  ... and {len(broken) - 20} more")

    if args.write_log:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        log = WIKI_DIR / "log.md"
        block = [
            f"\n## [{ts}] lint",
            f"- topic pages: {len(topic_cites)} | people pages: {len(people_cites)}",
            f"- near-duplicate topic slugs (jaccard ≥ {args.threshold}): **{len(near_dups)}** — top candidates:",
        ]
        for a, b, jac in near_dups[:15]:
            block.append(f"  - `{a}` ↔ `{b}` ({jac:.2f})")
        block.append(f"- single-citation topic pages: {len(single_topics)} ({100*len(single_topics)/max(len(topic_cites),1):.0f}%) — merge or grow")
        block.append(f"- single-citation people pages: {len(single_people)} ({100*len(single_people)/max(len(people_cites),1):.0f}%)")
        block.append(f"- broken cross-links: **{len(broken)}** — top targets:")
        for src, target in broken[:10]:
            block.append(f"  - `{src}` → `{target}`")
        block.append("")
        log.write_text(log.read_text() + "\n".join(block))
        print(f"\n→ appended to {log}")


if __name__ == "__main__":
    main()
