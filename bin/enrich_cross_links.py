#!/usr/bin/env -S uv run python
"""Enrich a wiki episode page with embedding-based cross-link suggestions.

Looks at the hook + key-takeaways of the episode, finds the most-similar
existing topic and people pages by cosine, and adds high-confidence
matches to the page's `## Cross-links` section. Lower-confidence matches
go to stderr for human review.

Usage:
    ./bin/enrich_cross_links.py <slug>                 # one episode
    ./bin/enrich_cross_links.py --recent 50            # most-recent 50 by mtime
    ./bin/enrich_cross_links.py --all --dry-run        # preview, no writes
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from podmind.enrich import enrich_episode, AUTO_THRESHOLD, SUGGEST_THRESHOLD
from _lib import WIKI_DIR


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("slug", nargs="?", help="single episode slug (no .md)")
    g.add_argument("--recent", type=int, help="enrich the N most-recently-modified episodes")
    g.add_argument("--all", action="store_true", help="enrich every episode")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, no writes")
    ap.add_argument("--auto-threshold", type=float, default=AUTO_THRESHOLD)
    ap.add_argument("--suggest-threshold", type=float, default=SUGGEST_THRESHOLD)
    args = ap.parse_args()

    eps_dir = WIKI_DIR / "episodes"
    if args.slug:
        targets = [eps_dir / f"{args.slug}.md"]
    elif args.recent:
        targets = sorted(eps_dir.glob("*.md"), key=lambda p: -p.stat().st_mtime)[:args.recent]
    else:
        targets = sorted(eps_dir.glob("*.md"))

    total_added_t = 0
    total_added_p = 0
    total_suggested = 0
    skipped = 0

    for page in targets:
        if not page.exists():
            print(f"  ✗ not found: {page.name}", file=sys.stderr)
            continue
        report = enrich_episode(
            page,
            auto_threshold=args.auto_threshold,
            suggest_threshold=args.suggest_threshold,
            dry_run=args.dry_run,
        )
        added = report["added_topics"] + report["added_people"]
        if not added and not (report["suggested_topics"] or report["suggested_people"]):
            skipped += 1
            continue
        total_added_t += len(report["added_topics"])
        total_added_p += len(report["added_people"])
        total_suggested += len(report["suggested_topics"]) + len(report["suggested_people"])

        prefix = "[dry-run]" if args.dry_run else "[enriched]"
        if added:
            print(f"{prefix} {page.stem}")
            for s in report["added_topics"]:
                print(f"    + topics/{s}")
            for s in report["added_people"]:
                print(f"    + people/{s}")
        for slug, score in report["suggested_topics"]:
            print(f"  ? topics/{slug} ({score:.2f}) — review", file=sys.stderr)
        for slug, score in report["suggested_people"]:
            print(f"  ? people/{slug} ({score:.2f}) — review", file=sys.stderr)

    verb = "would add" if args.dry_run else "added"
    print()
    print(f"summary: {verb} {total_added_t} topic + {total_added_p} people cross-links across {len(targets) - skipped} episodes")
    print(f"         {total_suggested} suggestions logged to stderr (in [{args.suggest_threshold}, {args.auto_threshold}) cosine band)")


if __name__ == "__main__":
    main()
