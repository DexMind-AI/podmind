#!/usr/bin/env -S uv run python
"""tick_finalize — run the writer over /tmp/podmind-results/, regen index, append log.

Usage: ./bin/tick_finalize.py <batch_number> [--note "<freeform line>"] [--quarantined-corrupt RD:reason ...]
"""
import argparse
import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from podmind.paths import DATA_ROOT as ROOT, WIKI_DIR as WIKI, EPISODES_DIR as RAW_EP
RESULTS = Path("/tmp/podmind-results")


def _kebab(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:60]


def _person(p):
    if isinstance(p, dict):
        return [
            p.get("slug") or _kebab(p.get("name", "")),
            p.get("name", ""),
            p.get("role", ""),
            p.get("note", ""),
        ]
    if isinstance(p, str):
        return [_kebab(p), p, "", ""]
    if isinstance(p, (list, tuple)):
        items = list(p) + ["", "", "", ""]
        return items[:4]
    return ["", "", "", ""]


def _topic(t):
    if isinstance(t, dict):
        return [
            t.get("slug") or _kebab(t.get("name", "")),
            t.get("name", ""),
            t.get("why") or t.get("note", ""),
        ]
    if isinstance(t, str):
        return [_kebab(t), t, ""]
    if isinstance(t, (list, tuple)):
        items = list(t) + ["", "", ""]
        return items[:3]
    return ["", "", ""]


_TITLE_PREFIXES = ("dr-", "mr-", "mrs-", "ms-", "prof-", "professor-")


def _normalize_for_match(slug: str) -> str:
    s = slug
    for pre in _TITLE_PREFIXES:
        if s.startswith(pre):
            s = s[len(pre):]
            break
    if s.endswith("ies") and len(s) > 4:
        s = s[:-3] + "y"
    elif s.endswith("s") and len(s) > 3:
        s = s[:-1]
    return s


def find_canonical_slug(kind: str, slug: str, existing: set[str]) -> str | None:
    """Return existing slug if `slug` is a high-confidence duplicate, else None.

    High-confidence: token-subset (≥2 shared tokens) or plural/title-prefix match.
    Lower-confidence (difflib ≥0.85) is reported on stderr but not auto-redirected.
    """
    if not slug or slug in existing:
        return slug if slug in existing else None
    proposed_tokens = slug.split("-")
    parts = set(proposed_tokens)
    norm = _normalize_for_match(slug)
    for ex in sorted(existing):
        ex_tokens = ex.split("-")
        ex_parts = set(ex_tokens)
        shared = parts & ex_parts
        if len(shared) >= 2 and (parts <= ex_parts or ex_parts <= parts):
            return ex
        if _normalize_for_match(ex) == norm:
            return ex
        # Prefix-path rule: only safe for topics (refinement). For people, longer
        # names = different humans (e.g., stephen-bartlett vs a stub stephen.md).
        if (
            kind == "topics"
            and len(ex_tokens) < len(proposed_tokens)
            and proposed_tokens[: len(ex_tokens)] == ex_tokens
        ):
            return ex
    matches = difflib.get_close_matches(slug, existing, n=1, cutoff=0.85)
    if matches:
        sys.stderr.write(
            f"[slug-lint] {kind}/{slug}: similar to existing {kind}/{matches[0]} "
            f"— review (no auto-redirect)\n"
        )
    return None


def canonicalize_slugs_inplace(eps: list[dict]) -> int:
    """Rewrite people/topic slugs to existing canonical forms when high-confidence
    duplicates exist on disk or in earlier episodes of this batch. Returns the
    number of redirects applied."""
    redirects = 0
    for kind, ep_key in (("people", "people"), ("topics", "topics")):
        existing = {p.stem for p in (WIKI / kind).glob("*.md")}
        for ep in eps:
            for entry in ep[ep_key]:
                slug = entry[0]
                canonical = find_canonical_slug(kind, slug, existing)
                if canonical and canonical != slug:
                    sys.stderr.write(
                        f"[slug-lint] redirect {kind}/{slug} → {kind}/{canonical}\n"
                    )
                    entry[0] = canonical
                    redirects += 1
                existing.add(entry[0])
    return redirects


def to_episode(d: dict) -> dict:
    """Adapt an LLM result dict to the writer's shape.

    Every field except raw_dir/show_slug/date (force-set by summarize from
    the dispatch, never LLM-controlled) comes from the LLM and may be absent
    on a truncated/malformed response. Missing optional fields degrade to
    empty; missing REQUIRED fields raise ValueError naming the episode so the
    caller can skip just that result instead of KeyError-crashing the batch
    (which left partial writes + a daily re-summarize loop, 2026-06-12 review).
    """
    missing = [k for k in ("raw_dir", "show_slug", "date") if not d.get(k)]
    if missing:
        raise ValueError(f"result missing dispatch-set fields {missing}: {str(d)[:120]}")
    show_slug = d["show_slug"]
    quotes = []
    for q in d.get("quotes") or []:
        if isinstance(q, dict):
            txt = q.get("text", "")
            attr = q.get("attribution") or q.get("speaker") or ""
            ts = q.get("timestamp")
            spk = f"{attr} ({ts})" if ts else attr
            quotes.append([spk, txt])
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            quotes.append([q[0], q[1]])
    # The LLM occasionally returns title_slug > 50 chars despite the prompt
    # rule. macOS caps a path component at 255 bytes; with show_slug + date
    # + ".md" we have ~200 bytes for the title_slug. Truncate hard at 60.
    # (Observed 2026-06-02 on DeepSeek V4: a 250-char "boys-club ... wi-fi-for-fishermen
    # ..." title crashed the batch on episode N of 2000.)
    title_slug = (d.get("title_slug") or "untitled")[:60].rstrip("-")
    # Hook is rendered on the badge line — embedded newlines would break
    # first_body_line extraction and Obsidian rendering. Collapse whitespace.
    hook = " ".join((d.get("hook") or "").split())
    return {
        "raw_dir": d["raw_dir"],
        "show": d.get("show_name") or show_slug.replace("-", " ").title(),
        "show_link": f"shows/{show_slug}",
        "filename": f"{show_slug}-{d['date']}-{title_slug}.md",
        "guests": d.get("guests") or [],
        "hook": hook,
        "takeaways": d.get("takeaways") or [],
        "quotes": quotes,
        "people": [_person(p) for p in d.get("people") or []],
        "topics": [_topic(t) for t in d.get("topics") or []],
    }


def load_meta(rd: str) -> dict:
    return json.loads((RAW_EP / rd / "meta.json").read_text())


def badge(meta: dict) -> str:
    if meta.get("listened"):
        return "🎧"
    pu = meta.get("played_up_to", 0)
    # Schema key is duration_sec (docs/AGENTS-vault.md, pocketcasts.py, youtube.py).
    # This read `duration` for months — always 0 — so every page got
    # duration_min: 0 and the ▶ NN% badge never computed. Review 2026-06-12.
    dur = meta.get("duration_sec", 0)
    if pu and dur:
        return f"▶ {int(100*pu/dur)}%"
    if pu:
        return "▶"
    return "⚪"


def _yaml_str(s: str) -> str:
    """Quote a scalar so it's valid YAML. Show names like 'Real Vision:
    Finance & Investing' contain colons that break unquoted YAML — that
    silently corrupted 992 pages' frontmatter, making them unreadable by
    read_raw_dir and thus re-ingested daily forever (caught 2026-06-11).
    Double-quote and escape embedded quotes/backslashes."""
    s = str(s)
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_episode(ep: dict) -> Path:
    meta = load_meta(ep["raw_dir"])
    bdg = badge(meta)
    dur = meta.get("duration_sec", 0)
    dur_min = int(dur / 60) if dur else 0
    listened = meta.get("listened", False)
    pu = meta.get("played_up_to", 0)
    pub = (meta.get("pub_date") or meta.get("watched_at") or "")[:10]
    src = meta.get("transcript_source", "")
    guests_yaml = "[" + ", ".join(_yaml_str(g) for g in ep["guests"]) + "]" if ep["guests"] else "[]"
    lines = [
        "---",
        f"show: {_yaml_str(ep['show'])}",
        f"date: {pub}",
        f"listened: {'true' if listened else 'false'}",
        f"played_up_to: {pu}",
        f"duration_min: {dur_min}",
        f"guests: {guests_yaml}",
        f"transcript_source: {_yaml_str(src) if src else 'null'}",
        f"raw_dir: {_yaml_str(ep['raw_dir'])}",
        "---",
        "",
        f"{bdg} [[{ep['show_link']}]] — {ep['hook']}",
        "",
        "## Key takeaways",
        "",
    ]
    lines += [f"- {t}" for t in ep["takeaways"]]
    lines.append("")
    if ep["quotes"]:
        lines.append("## Notable quotes")
        lines.append("")
        for spk, txt in ep["quotes"]:
            lines.append(f"> {txt} — {spk}")
            lines.append("")
    lines.append("## Cross-links")
    lines.append("")
    if ep["people"]:
        ppl = ", ".join(f"[[people/{slug}]]" for slug, *_ in ep["people"])
        lines.append(f"- People: {ppl}")
    if ep["topics"]:
        tps = ", ".join(f"[[topics/{slug}]]" for slug, *_ in ep["topics"])
        lines.append(f"- Topics: {tps}")
    lines.append("")
    out = WIKI / "episodes" / ep["filename"]
    text = "\n".join(lines)

    # Write-then-verify: parse the frontmatter we just produced and confirm
    # raw_dir round-trips. If it doesn't, the page would be invisible to
    # tick_prep.load_ingested and get re-summarized forever (the colon-in-
    # show-name bug, 2026-06-11). Three silent-corruption bugs this class
    # in one session — fail loud at the source instead.
    from podmind.frontmatter import parse
    fm, _ = parse(text)
    if fm.get("raw_dir") != ep["raw_dir"]:
        raise ValueError(
            f"frontmatter round-trip failed for {ep['filename']}: "
            f"raw_dir read back as {fm.get('raw_dir')!r}, expected "
            f"{ep['raw_dir']!r}. Likely an unquoted YAML-special char in "
            f"show={ep['show']!r}. Refusing to write a page tick_prep can't see."
        )

    out.write_text(text)
    return out


def ensure_stub(kind: str, slug: str, name: str, note: str, why: str, ep_filename: str, hook: str) -> None:
    p = WIKI / kind / f"{slug}.md"
    citation = f"- [[episodes/{ep_filename[:-3]}]] — {hook[:120]}"
    if p.exists():
        text = p.read_text()
        if ep_filename[:-3] not in text:
            if "## Citations" in text:
                text = text.replace("## Citations", "## Citations\n\n" + citation, 1)
            else:
                text = text.rstrip() + "\n\n## Citations\n\n" + citation + "\n"
            p.write_text(text)
    else:
        if kind == "people":
            body = f"# {name}\n\n_{note}._\n\n{why}\n\n## Citations\n\n{citation}\n"
        else:
            body = f"# {name}\n\n{why}\n\n## Citations\n\n{citation}\n"
        p.write_text(body)


def update_show(
    show_link: str, ep_filename: str, date: str, bdg: str, hook: str, show_name: str = ""
) -> None:
    p = WIKI / "shows" / f"{show_link.split('/')[-1]}.md"
    bullet = f"- [{date}] [[episodes/{ep_filename[:-3]}]] {bdg} — {hook[:120]}"
    if not p.exists():
        title = show_name or show_link.split("/")[-1].replace("-", " ").title()
        p.write_text(f"# {title}\n\n## Episodes\n\n{bullet}\n")
        return
    text = p.read_text()
    if ep_filename[:-3] in text:
        return
    if "## Episodes" in text:
        text = text.replace("## Episodes\n\n", f"## Episodes\n\n{bullet}\n", 1)
    else:
        text = text.rstrip() + f"\n\n## Episodes\n\n{bullet}\n"
    p.write_text(text)


def regenerate_index() -> tuple[int, int, int, int]:
    shows = sorted(p.stem for p in (WIKI / "shows").glob("*.md"))
    people = sorted(p.stem for p in (WIKI / "people").glob("*.md"))
    topics = sorted(p.stem for p in (WIKI / "topics").glob("*.md"))
    from podmind.frontmatter import EpisodePage
    eps = []
    for p in (WIKI / "episodes").glob("*.md"):
        ep = EpisodePage.from_file(p)
        eps.append((ep.date or "0000-00-00", p.stem, ep.hook))
    eps.sort(reverse=True)

    synthesis_section = ""
    idx_path = WIKI / "index.md"
    if idx_path.exists():
        old = idx_path.read_text()
        m = re.search(r"## Synthesis\n.*?(?=\n## |\Z)", old, re.S)
        if m:
            synthesis_section = m.group(0).rstrip() + "\n\n"

    lines = [
        "# Podcast Wiki — Index",
        "",
        "Karpathy-style LLM-built knowledge wiki of podcast subscriptions. Updated incrementally; one log entry per ingest run.",
        "",
        "## Shows", "",
    ]
    lines += [f"- [[shows/{s}]]" for s in shows]
    lines += ["", "## People", ""] + [f"- [[people/{s}]]" for s in people]
    lines += ["", "## Topics", ""] + [f"- [[topics/{s}]]" for s in topics]
    lines += ["", f"## Recent episodes ({len(eps)} total)", ""]
    for date, stem, first in eps[:200]:
        lines.append(f"- [{date}] [[episodes/{stem}]] — {first[:140]}")
    lines.append("")
    if synthesis_section:
        lines.append(synthesis_section.rstrip())
        lines.append("")
    lines += ["---", "", "See `log.md` for the running ingest chronicle."]
    idx_path.write_text("\n".join(lines))
    return len(shows), len(people), len(topics), len(eps)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch", type=int, help="batch number for log entry")
    ap.add_argument("--note", default="", help="freeform highlights line")
    ap.add_argument("--corrupt", action="append", default=[], help="raw_dir:reason for quarantined corrupt transcripts")
    ap.add_argument("--quarantined-dups", action="append", default=[], help="raw_dir:dup_of pairs (informational only)")
    args = ap.parse_args()

    # Apply corruption quarantines first (rename transcript files + meta flag)
    for spec in args.corrupt:
        rd, _, reason = spec.partition(":")
        raw = RAW_EP / rd
        m = raw / "meta.json"
        if m.exists():
            from podmind.jsonio import write_json_atomic
            d = json.loads(m.read_text())
            d["transcript_source"] = "none"
            d["transcript_corrupt_reason"] = reason or "yt-dlp transcript mismatch"
            write_json_atomic(m, d)
            for f in raw.glob("transcript*"):
                if not f.name.endswith(".corrupt"):
                    f.rename(str(f) + ".corrupt")

    # Skip results corresponding to corruption-quarantined raw_dirs
    skip_rd = {spec.split(":", 1)[0] for spec in args.corrupt}

    eps = []
    # Skip sidecar files (e.g. _cost.json written by summarize). The
    # results dir is shared with cost telemetry; only numeric-prefixed
    # files are episode results.
    for jp in sorted(p for p in RESULTS.glob("*.json") if not p.name.startswith("_")):
        try:
            d = json.loads(jp.read_text())
        except json.JSONDecodeError:
            print(f"BAD JSON: {jp}", file=sys.stderr)
            continue
        if d.get("raw_dir") in skip_rd:
            continue
        try:
            eps.append(to_episode(d))
        except (ValueError, KeyError, TypeError) as e:
            # One malformed LLM result must not abort the batch — partial
            # writes plus an unfinished log entry put the remaining episodes
            # on a daily re-summarize treadmill. Skip it loudly; it stays
            # pending and gets retried (fresh LLM call) next run.
            print(f"SKIP {jp.name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue

    redirects = canonicalize_slugs_inplace(eps)

    written = []
    skipped_writes = 0
    for ep in eps:
        try:
            out = write_episode(ep)
            meta = load_meta(ep["raw_dir"])
        except (ValueError, OSError) as e:
            # write_episode's round-trip guard, a too-long filename, or a
            # vanished meta.json. Skip THIS episode (stays pending, retried
            # next run) rather than aborting the batch mid-write.
            print(f"SKIP write {ep.get('filename', ep['raw_dir'])}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            skipped_writes += 1
            continue
        bdg = badge(meta)
        pub = (meta.get("pub_date") or meta.get("watched_at") or "")[:10]
        for slug, name, role, note in ep["people"]:
            ensure_stub("people", slug, name, role, note, out.name, ep["hook"])
        for slug, name, why in ep["topics"]:
            ensure_stub("topics", slug, name, "", why, out.name, ep["hook"])
        update_show(ep["show_link"], out.name, pub, bdg, ep["hook"], show_name=ep["show"])
        written.append((out.name, ep, bdg, pub))
    if skipped_writes:
        print(f"WARNING: {skipped_writes} episode write(s) skipped — see SKIP lines above",
              file=sys.stderr)

    n_shows, n_people, n_topics, n_eps = regenerate_index()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    log = WIKI / "log.md"
    existing_log = log.read_text() if log.exists() else "# Wiki log\n\n"
    block = [
        f"\n## [{ts}] ingest",
        f"- batch {args.batch} — autonomous loop",
        f"- episodes ingested: {len(written)}",
    ]
    if args.note:
        block.append(f"- highlights: {args.note}")
    if args.corrupt:
        block.append("- corrupt transcripts quarantined:")
        for spec in args.corrupt:
            rd, _, reason = spec.partition(":")
            block.append(f"  - {rd} ({reason})")
    if args.quarantined_dups:
        block.append("- yt-dlp dups quarantined:")
        for spec in args.quarantined_dups:
            block.append(f"  - {spec}")
    block.append("")
    block.append("Episodes:")
    for stem, ep, bdg, pub in written:
        block.append(f"- [{pub}] {bdg} [[episodes/{stem[:-3]}]] — {ep['hook'][:100]}")
    block.append("")
    block.append(f"- totals: {n_eps} episodes / {n_shows} shows / {n_people} people / {n_topics} topics")
    log.write_text(existing_log + "\n".join(block))

    print(f"Wrote {len(written)} episode pages.")
    print(f"Index: {n_shows} shows, {n_people} people, {n_topics} topics, {n_eps} episodes")
    if redirects:
        print(f"Slug-lint: {redirects} slug redirect(s) applied to existing canonical pages.")
    print(f"Log entry [{ts}] appended.")


if __name__ == "__main__":
    main()
