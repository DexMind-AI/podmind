#!/usr/bin/env -S uv run python
"""Find people/topic pages cited by episodes that don't exist yet.

Walks every wiki/episodes/*.md, collects `[[people/<slug>]]` and
`[[topics/<slug>]]` references, and reports the targets that have no
corresponding file. Optionally creates minimal stub pages from the
citing episodes so the links resolve (the stubs get fleshed out later
by future ingests of the same person/topic).

Usage:
    ./bin/audit_missing_pages.py                  # report only
    ./bin/audit_missing_pages.py --create-stubs   # create stub pages
    ./bin/audit_missing_pages.py --kind people    # restrict to one kind
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from podmind.frontmatter import parse_file
from _lib import WIKI_DIR


LINK_RE = {
    "people": re.compile(r"\[\[people/([^\]|]+)\]\]"),
    "topics": re.compile(r"\[\[topics/([^\]|]+)\]\]"),
}


def find_missing(kind: str) -> dict[str, list[tuple[str, str]]]:
    """Return {missing_slug: [(citing_episode_stem, hook_snippet), ...]}.

    Skips Obsidian-convention hidden files (leading underscore) and placeholder
    targets (`...` or anything with non-slug characters)."""
    existing = {p.stem for p in (WIKI_DIR / kind).glob("*.md")}
    pat = LINK_RE[kind]
    missing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ep_path in (WIKI_DIR / "episodes").glob("*.md"):
        if ep_path.name.startswith("_"):
            continue
        _, body = parse_file(ep_path)
        hook = next((l.strip() for l in body.splitlines() if l.strip()), "")[:160]
        for slug in pat.findall(body):
            # Skip obvious template placeholders
            if slug == "..." or "<" in slug or ">" in slug:
                continue
            if slug not in existing:
                missing[slug].append((ep_path.stem, hook))
    return missing


def slug_to_label(slug: str) -> str:
    """`heinrich-bruning` → `Heinrich Brüning` — naive but works for the
    common case; the LLM will improve it when the page is ingested."""
    return " ".join(w.capitalize() for w in slug.split("-"))


def create_stub(kind: str, slug: str, citations: list[tuple[str, str]]) -> Path:
    p = WIKI_DIR / kind / f"{slug}.md"
    label = slug_to_label(slug)
    lines = [
        "---",
        f"name: {label}",
        f"auto_stub: true",
        "---",
        "",
        f"# {label}",
        "",
        f"_Auto-created stub. Cited by {len(citations)} episode(s) but no detailed page yet._",
        "",
        "## Citations",
        "",
    ]
    for ep_stem, hook in citations:
        lines.append(f"- [[episodes/{ep_stem}]] — {hook[:120]}")
    lines.append("")
    p.write_text("\n".join(lines))
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["people", "topics", "both"], default="both")
    ap.add_argument("--create-stubs", action="store_true",
                    help="create minimal stub pages for the missing slugs")
    ap.add_argument("--min-citations", type=int, default=1,
                    help="only report/create slugs cited at least N times (default 1)")
    args = ap.parse_args()

    kinds = ["people", "topics"] if args.kind == "both" else [args.kind]
    total_missing = 0
    total_created = 0

    for kind in kinds:
        missing = find_missing(kind)
        # Sort by citation count desc — most-cited missing pages first
        items = sorted(missing.items(), key=lambda kv: -len(kv[1]))
        items = [(s, c) for s, c in items if len(c) >= args.min_citations]
        print(f"\n=== {kind} ({len(items)} missing, cited ≥{args.min_citations}× each) ===")
        for slug, citations in items[:30]:
            verb = "stub" if args.create_stubs else "missing"
            print(f"  [{len(citations):>2}×]  {kind}/{slug}  ({verb})")
            if args.create_stubs:
                create_stub(kind, slug, citations)
                total_created += 1
        if len(items) > 30:
            print(f"  ... and {len(items) - 30} more")
        total_missing += len(items)

    print()
    if args.create_stubs:
        print(f"created {total_created} stub pages")
    else:
        print(f"total missing: {total_missing} — re-run with --create-stubs to fill in")


if __name__ == "__main__":
    main()
