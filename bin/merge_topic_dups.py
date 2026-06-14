#!/usr/bin/env -S uv run python
"""merge_topic_dups — collapse near-duplicate topic slugs into a canonical one.

Workflow:
1. Find pairs with token-set jaccard ≥ threshold.
2. Group transitively (a↔b, b↔c → {a,b,c}).
3. Pick canonical: most-cited member wins; tie → shortest slug.
4. Rewrite every `[[topics/<non-canonical>]]` reference across wiki/.
5. Append unique citations from non-canonical pages to canonical's `## Citations`.
6. Delete non-canonical topic files.
7. Append a `## [date] merge` log entry.

Usage:
  ./bin/merge_topic_dups.py --dry-run           # preview, no changes
  ./bin/merge_topic_dups.py --apply             # do it
  ./bin/merge_topic_dups.py --threshold 0.9     # default 1.0 (exact-equal token sets)
"""
import argparse
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from _lib import WIKI_DIR


from podmind.slugs import slug_tokens, SLUG_STOPWORDS  # noqa: F401


def find_pairs(threshold: float) -> list[tuple[str, str, float]]:
    topics = [p.stem for p in (WIKI_DIR / "topics").glob("*.md")]
    by_first: dict[str, list[str]] = defaultdict(list)
    for t in topics:
        toks = sorted(slug_tokens(t))
        if toks:
            by_first[toks[0]].append(t)
    pairs = []
    for bucket in by_first.values():
        if len(bucket) < 2:
            continue
        for i, a in enumerate(bucket):
            ta = slug_tokens(a)
            if not ta: continue
            for b in bucket[i + 1:]:
                tb = slug_tokens(b)
                if not tb: continue
                jac = len(ta & tb) / len(ta | tb)
                if jac >= threshold:
                    pairs.append((a, b, jac))
    return pairs


def union_find(pairs: list[tuple[str, str, float]]) -> list[set[str]]:
    parent: dict[str, str] = {}
    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for a, b, _ in pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)
    groups: dict[str, set[str]] = defaultdict(set)
    for k in parent:
        groups[find(k)].add(k)
    return [g for g in groups.values() if len(g) > 1]


def count_citations(slug: str) -> int:
    p = WIKI_DIR / "topics" / f"{slug}.md"
    if not p.exists():
        return 0
    return len(re.findall(r"\[\[episodes/", p.read_text(errors="ignore")))


def pick_canonical(group: set[str]) -> str:
    return min(group, key=lambda s: (-count_citations(s), len(s), s))


def rewrite_references(group: set[str], canonical: str, dry_run: bool) -> int:
    """Rewrite [[topics/<old>]] → [[topics/<canonical>]] across wiki/."""
    others = group - {canonical}
    if not others:
        return 0
    pat = re.compile(
        r"\[\[topics/(" + "|".join(re.escape(s) for s in sorted(others, key=len, reverse=True)) + r")\]\]"
    )
    affected = 0
    for kind in ("episodes", "people", "topics", "shows", "synthesis"):
        d = WIKI_DIR / kind
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            text = f.read_text(errors="ignore")
            new = pat.sub(f"[[topics/{canonical}]]", text)
            if new != text:
                affected += 1
                if not dry_run:
                    f.write_text(new)
    return affected


def merge_citations(group: set[str], canonical: str, dry_run: bool) -> None:
    """Append unique [[episodes/...]] citations from non-canonical pages to canonical's."""
    canon_path = WIKI_DIR / "topics" / f"{canonical}.md"
    if not canon_path.exists():
        return
    canon_text = canon_path.read_text(errors="ignore")
    canon_eps = set(re.findall(r"\[\[episodes/([^\]]+)\]\]", canon_text))
    new_lines: list[str] = []
    for slug in sorted(group - {canonical}):
        p = WIKI_DIR / "topics" / f"{slug}.md"
        if not p.exists():
            continue
        for m in re.finditer(r"^(- \[\[episodes/([^\]]+)\]\][^\n]*)$", p.read_text(errors="ignore"), re.M):
            line, ep = m.group(1), m.group(2)
            if ep not in canon_eps:
                canon_eps.add(ep)
                new_lines.append(line)
    if new_lines and not dry_run:
        if "## Citations" in canon_text:
            canon_text = canon_text.replace(
                "## Citations\n\n",
                "## Citations\n\n" + "\n".join(new_lines) + "\n",
                1,
            )
        else:
            canon_text = canon_text.rstrip() + "\n\n## Citations\n\n" + "\n".join(new_lines) + "\n"
        canon_path.write_text(canon_text)


def delete_old(group: set[str], canonical: str, dry_run: bool) -> int:
    n = 0
    for slug in group - {canonical}:
        p = WIKI_DIR / "topics" / f"{slug}.md"
        if p.exists():
            n += 1
            if not dry_run:
                p.unlink()
    return n


PROPOSALS_PATH = WIKI_DIR / "_merge-proposals.md"


PROPOSAL_HEADER = """# Merge proposals — review-then-apply

_Generated by `bin/merge_topic_dups.py --propose --threshold {threshold}`._

**Reading**:
- `[x] merge group` = approve this group. Leave `[ ]` to skip.
- Each member is a checkbox: `[x]` = include in merge, `[ ]` = exclude.
- To pick a different canonical, edit the `canonical:` line.

**Note on Obsidian visuals**: Obsidian by default strikes-through ticked tasks. If that's distracting, drop `wiki/_obsidian-snippets/no-strikethrough.css` into your vault's `.obsidian/snippets/` and enable it in Settings → Appearance → CSS snippets. (The snippet is in this repo for convenience.)

When done, run `./bin/merge_topic_dups.py --apply-from {rel_path}`.

**Total proposals**: {n_groups} groups (threshold ≥ {threshold})

---

"""


def write_proposals(groups: list[set[str]], threshold: float) -> None:
    rel = PROPOSALS_PATH.relative_to(WIKI_DIR.parent)
    lines = [PROPOSAL_HEADER.format(threshold=threshold, rel_path=rel, n_groups=len(groups))]
    sorted_groups = sorted(groups, key=lambda g: -sum(count_citations(s) for s in g))
    for i, group in enumerate(sorted_groups, 1):
        canonical = pick_canonical(group)
        cites = sorted(((s, count_citations(s)) for s in group), key=lambda x: -x[1])
        lines.append(f"## {i}. `{canonical}`\n")
        lines.append("- [ ] merge group")
        lines.append(f"- canonical: `{canonical}`")
        lines.append("- members:")
        for s, n in cites:
            tag = " ← canonical" if s == canonical else ""
            lines.append(f"  - [x] `{s}` ({n} citations){tag}")
        lines.append("")
    PROPOSALS_PATH.write_text("\n".join(lines))


def parse_proposals(path: Path) -> list[tuple[set[str], str]]:
    """Parse a reviewed proposals file via the canonical podmind.proposals parser.

    Returns (members, canonical) tuples to preserve the existing call-site shape
    in main(). New code should use `podmind.proposals.parse_file` directly and
    consume `Proposal` objects.
    """
    from podmind.proposals import parse_file
    return [(set(p.members), p.canonical) for p in parse_file(path)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=1.0,
                    help="token-jaccard threshold (default 1.0 = exact-equal token sets, safe)")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview changes without writing — works alongside --apply or --apply-from")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--apply", action="store_true",
                   help="merge all groups at the chosen --threshold")
    g.add_argument("--propose", action="store_true",
                   help=f"write proposals as a checklist to {PROPOSALS_PATH.relative_to(WIKI_DIR.parent)} for human review in Obsidian")
    g.add_argument("--apply-from", metavar="PATH",
                   help="read a reviewed proposals file and merge only the ticked groups")
    args = ap.parse_args()

    if args.apply_from:
        proposals = parse_proposals(Path(args.apply_from))
        action = "would merge" if args.dry_run else "merging"
        print(f"parsed {len(proposals)} ticked groups from {args.apply_from} — {action}")
        if not proposals:
            print("nothing ticked — exiting")
            return
        total_files = 0
        total_deleted = 0
        summary: list[tuple[str, set[str], int]] = []
        for members, canonical in proposals:
            affected = rewrite_references(members, canonical, dry_run=args.dry_run)
            merge_citations(members, canonical, dry_run=args.dry_run)
            deleted = delete_old(members, canonical, dry_run=args.dry_run)
            total_files += affected
            total_deleted += deleted
            summary.append((canonical, members, affected))
            cite_str = ", ".join(f"{s}({count_citations(s)})" for s in sorted(members))
            verb = "would touch" if args.dry_run else "touched"
            verb_d = "would delete" if args.dry_run else "deleted"
            print(f"  → {canonical}  ({cite_str})  {verb}:{affected}  {verb_d}:{deleted}")
        if args.dry_run:
            print(f"\n[DRY RUN] would touch {total_files} files, delete {total_deleted} topic pages.")
            print("Re-run without --dry-run to apply.")
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        log = WIKI_DIR / "log.md"
        block = [
            f"\n## [{ts}] merge",
            f"- merged {len(proposals)} groups from reviewed proposals at `{args.apply_from}`",
            f"- {total_files} wiki files updated; {total_deleted} topic pages removed",
        ]
        for canonical, members, _ in summary[:30]:
            others = sorted(members - {canonical})
            block.append(f"  - `{canonical}` ← {', '.join(f'`{s}`' for s in others)}")
        log.write_text(log.read_text() + "\n".join(block) + "\n")
        print(f"\n→ logged to {log}")
        return

    pairs = find_pairs(args.threshold)
    groups = union_find(pairs)
    print(f"Found {len(pairs)} pairs forming {len(groups)} groups (threshold {args.threshold})")
    if not groups:
        return

    if args.propose:
        write_proposals(groups, args.threshold)
        rel = PROPOSALS_PATH.relative_to(WIKI_DIR.parent)
        print(f"→ wrote {rel}  — open in Obsidian, tick groups to merge, then run:")
        print(f"   ./bin/merge_topic_dups.py --apply-from {rel}")
        return

    print()

    total_files = 0
    total_deleted = 0
    summary: list[tuple[str, set[str], int]] = []
    for group in sorted(groups, key=lambda g: -sum(count_citations(s) for s in g)):
        canonical = pick_canonical(group)
        cites = {s: count_citations(s) for s in group}
        affected = rewrite_references(group, canonical, dry_run=args.dry_run)
        merge_citations(group, canonical, dry_run=args.dry_run)
        deleted = delete_old(group, canonical, dry_run=args.dry_run)
        total_files += affected
        total_deleted += deleted
        summary.append((canonical, group, affected))
        cite_str = ", ".join(f"{s}({n})" for s, n in sorted(cites.items(), key=lambda x: -x[1]))
        print(f"  → {canonical}  ({cite_str})  files:{affected}  deleted:{deleted}")

    action = "would touch" if args.dry_run else "touched"
    print()
    print(f"{action} {total_files} wiki files; {action.replace('would ', 'would-').replace('touched','removed')} {total_deleted} topic pages")

    if args.apply and not args.dry_run:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        log = WIKI_DIR / "log.md"
        block = [
            f"\n## [{ts}] merge",
            f"- merged {len(groups)} near-duplicate topic groups (jaccard ≥ {args.threshold})",
            f"- {total_files} wiki files updated; {total_deleted} topic pages removed",
            "- groups (canonical → merged):",
        ]
        for canonical, group, affected in summary[:30]:
            others = sorted(group - {canonical})
            block.append(f"  - `{canonical}` ← {', '.join(f'`{s}`' for s in others)} ({affected} refs)")
        if len(summary) > 30:
            block.append(f"  - ... and {len(summary) - 30} more")
        log.write_text(log.read_text() + "\n".join(block) + "\n")
        print(f"→ logged to {log}")


if __name__ == "__main__":
    main()
