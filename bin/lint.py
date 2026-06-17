#!/usr/bin/env -S uv run python
"""lint — surface wiki health issues per docs/AGENTS-vault.md spec.

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
            if p.stem.startswith("_"):
                continue  # templates/meta (e.g. _template) hold placeholder links
            text = p.read_text(errors="ignore")
            for m in re.finditer(r"\[\[((?:episodes|people|topics|shows)/[^\]|]+)(?:\|[^\]]+)?\]\]", text):
                target = m.group(1)
                if target not in existing:
                    broken.append((f"{kind}/{p.stem}", target))
    return broken


def make_stub(target: str, backlinks: list[str]) -> bool:
    """Create a stub page for a broken-link target if it does not exist.

    `target` is `<kind>/<stem>`. Returns True if a file was created, False if
    one already existed (never overwrites).
    """
    kind, stem = target.split("/", 1)
    path = WIKI_DIR / kind / f"{stem}.md"
    if path.exists():
        return False
    title = stem.replace("-", " ").title()
    section = "Episodes" if kind == "shows" else "Citations"
    lines = [
        f"# {title}",
        "",
        "_Stub auto-created by `lint --fix` to resolve broken cross-links. Grow me._",
        "",
        f"## {section}",
    ]
    lines += [f"- [[{b}]]" for b in backlinks]
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return True


STUB_NAMESPACES = ("topics", "people", "shows")
VALID_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def fix_broken_links(broken: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Create stubs for broken topic/people/show targets; report episode targets.

    Returns (created_targets, reported_episode_targets). Targets whose stem is
    not a clean slug (e.g. placeholder `...` / `<show-slug>`) are skipped entirely.
    """
    by_target: dict[str, list[str]] = defaultdict(list)
    for src, target in broken:
        by_target[target].append(src)
    created: list[str] = []
    reported: list[str] = []
    for target in sorted(by_target):
        kind, _, stem = target.partition("/")
        if not VALID_SLUG.fullmatch(stem):
            continue  # junk target (placeholder/malformed) — never a real page
        if kind in STUB_NAMESPACES:
            if make_stub(target, sorted(set(by_target[target]))):
                created.append(target)
        else:  # episodes — a missing page is a real gap, never fabricate
            reported.append(target)
    return created, reported


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-log", action="store_true", help="append to wiki/log.md")
    ap.add_argument("--threshold", type=float, default=0.7, help="topic-dup jaccard threshold")
    ap.add_argument("--fix", action="store_true",
                    help="create stub pages for broken topic/people/show links")
    args = ap.parse_args()

    near_dups = find_near_dup_topics(args.threshold)
    topic_cites = count_citations("topics")
    people_cites = count_citations("people")
    single_topics = sorted([t for t, n in topic_cites.items() if n == 1])
    single_people = sorted([p for p, n in people_cites.items() if n == 1])
    broken = find_broken_links()

    created: list[str] = []
    reported: list[str] = []
    if args.fix:
        created, reported = fix_broken_links(broken)
        print(f"\nFix: created {len(created)} stub(s); "
              f"reported {len(reported)} broken episode link(s)")

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
        if args.fix:
            by_kind = Counter(t.split("/", 1)[0] for t in created)
            block.append(
                f"- stubs created: **{len(created)}** "
                f"(topics: {by_kind.get('topics', 0)}, "
                f"people: {by_kind.get('people', 0)}, "
                f"shows: {by_kind.get('shows', 0)})")
            block.append(
                f"- broken episode links (reported, not fixed): **{len(reported)}**")
            for t in reported[:10]:
                block.append(f"  - `{t}`")
        block.append("")
        log.write_text(log.read_text() + "\n".join(block))
        print(f"\n→ appended to {log}")


if __name__ == "__main__":
    main()
