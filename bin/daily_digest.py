#!/usr/bin/env -S uv run python
"""Daily digest — a 3-minute read of what you actually listened to.

Unions two sources of recent consumption — PocketCasts listening history and
a date/watched-at window over the wiki (which surfaces YouTube watches) — then
sends each episode's hook + key-takeaways to the configured LLM provider, which
clusters them into narrative threads:

  - Narrative threads across the day's episodes (the through-lines)
  - Per-episode hook with listened-state badge and source link
  - Uncategorized one-offs that don't fit a thread
  - People + topics that came up

Writes the digest to `wiki/digests/<date>.md` and optionally emails it
via Resend. Links from index.md under `## Digests`.

Usage:
    ./bin/daily_digest.py                       # last 24h, file only
    ./bin/daily_digest.py --hours 48            # widen the window
    ./bin/daily_digest.py --email you@me.com    # also email it
    ./bin/daily_digest.py --since 2026-05-09    # explicit date

Cost: ~$0.01 per digest (measured on DeepSeek V4 with ~10-20 episode snippets).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx

from podmind.frontmatter import EpisodePage
from podmind.digest_select import merge_episode_sources, is_engaged
from _lib import WIKI_DIR

RESEND_URL = "https://api.resend.com/emails"
DIGEST_MAX_EPISODES = 40

def collect_episodes(hours: int = 24, since: str | None = None,
                     max_n: int = DIGEST_MAX_EPISODES,
                     links_out: dict[str, str] | None = None) -> list[EpisodePage]:
    """Return EpisodePage objects for the user's recent listening AND watching.

    Unions two passes so the digest reflects everything consumed, not just
    PocketCasts:
      - `_from_pocketcasts_history`: PocketCasts /user/history (recency-ordered,
        real progress; also fills `links_out` with pca.st episode links).
      - `_from_pub_date_window`: a date/watched-at window over the wiki, which
        surfaces YouTube watches (`yt-*`) and anything PocketCasts history missed.

    `merge_episode_sources` dedupes by raw_dir (PocketCasts wins, preserving its
    pca.st link), sorts by date desc, and caps to `max_n`. Episodes already
    cited in a prior digest are excluded by both passes — first inclusion wins.
    """
    already = _previously_digested_slugs(skip=since or datetime.now().strftime("%Y-%m-%d"))
    primary = _from_pocketcasts_history(max_n, already, links_out)
    secondary = _from_pub_date_window(hours, since, already)
    kept, dropped = merge_episode_sources(primary, secondary, max_n=max_n)
    if dropped:
        print(f"  digest: capped to {max_n} episodes (dropped {dropped})", file=sys.stderr)
    return kept


def _previously_digested_slugs(skip: str) -> set[str]:
    """Episode slugs (filenames without .md) cited in any prior digest.

    `skip` is today's digest filename stem — excluded so a re-run on the same
    day re-evaluates today's listens rather than self-suppressing.
    """
    slugs: set[str] = set()
    digest_dir = WIKI_DIR / "digests"
    if not digest_dir.exists():
        return slugs
    for p in digest_dir.glob("*.md"):
        if p.stem == skip:
            continue
        for m in re.finditer(r"\[\[episodes/([^\]|#]+)\]\]", p.read_text(errors="ignore")):
            slugs.add(m.group(1).strip())
    return slugs


def _from_pocketcasts_history(max_n: int, already: set[str],
                              links_out: dict[str, str] | None = None) -> list[EpisodePage]:
    """Map PC /user/history → wiki episode pages. Empty list if PC unavailable."""
    try:
        from podmind import pocketcasts
        history = pocketcasts.listening_history()
    except Exception:
        return []
    if not history:
        return []

    # Build (show_uuid, pub_date) → list of wiki pages, plus a title fallback
    # The PC API's `uuid` is the internal episode id, NOT the RSS guid we store
    # in meta.json. Match by (show_uuid + published-date) instead — usually
    # unique within a show, with title-prefix as the tiebreaker.
    pc_index: dict[tuple[str, str], list[tuple[str, Path]]] = {}
    for p in (WIKI_DIR / "episodes").glob("*.md"):
        ep = EpisodePage.from_file(p)
        if not ep.raw_dir:
            continue
        meta_path = WIKI_DIR.parent / "raw" / "episodes" / ep.raw_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            m = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        show_uuid = m.get("show_uuid") or ""
        pub = (m.get("pub_date") or "")[:10]
        title = (m.get("title") or "").lower().strip()
        if show_uuid and pub:
            pc_index.setdefault((show_uuid, pub), []).append((title, p))

    out: list[EpisodePage] = []
    seen: set[Path] = set()
    for h in history:
        show_uuid = h.get("podcastUuid") or ""
        pub = (h.get("published") or "")[:10]
        title = (h.get("title") or "").lower().strip()
        cands = pc_index.get((show_uuid, pub), [])
        # Pick the page whose title starts the same (or just the only one)
        page = None
        if len(cands) == 1:
            page = cands[0][1]
        elif cands:
            for cand_title, cand_p in cands:
                if cand_title[:30] == title[:30]:
                    page = cand_p; break
            if page is None:
                page = cands[0][1]  # fallback to first
        if page is None or page in seen:
            continue
        seen.add(page)
        if page.stem in already:
            continue
        # Only include if meaningfully consumed (listened, or ≥50% played).
        ep = EpisodePage.from_file(page)
        if not is_engaged(ep):
            continue
        out.append(ep)
        ep_uuid = h.get("uuid")
        if links_out is not None and ep_uuid:
            links_out[page.stem] = f"https://pca.st/episode/{ep_uuid}"
        if len(out) >= max_n:
            break
    return out


def _from_pub_date_window(hours: int, since: str | None, already: set[str]) -> list[EpisodePage]:
    if since:
        cutoff_date = datetime.strptime(since, "%Y-%m-%d").date()
    else:
        cutoff_date = (datetime.now() - timedelta(hours=hours)).date()
    out: list[EpisodePage] = []
    for p in (WIKI_DIR / "episodes").glob("*.md"):
        if p.stem in already:
            continue
        ep = EpisodePage.from_file(p)
        if not ep.date:
            continue
        try:
            d = datetime.strptime(ep.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff_date:
            continue
        if not is_engaged(ep):
            continue
        out.append(ep)
    out.sort(key=lambda e: e.date or "", reverse=True)
    return out


def file_digest(date_str: str, content: str, n_episodes: int, hours: int) -> Path:
    """Write the digest body to wiki/digests/<date>.md with frontmatter.

    `content` is expected to be a complete markdown document (its own H1,
    sections, etc.) — typically the output of `threads.format_threads_md`.
    """
    out = WIKI_DIR / "digests" / f"{date_str}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"digest_date: {date_str}\n"
        f"generated_at: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"episodes_covered: {n_episodes}\n"
        f"window_hours: {hours}\n"
        "---\n\n"
    )
    out.write_text(frontmatter + content + "\n")
    return out


def send_email(to: str, subject: str, html_body: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY")
    from_addr = os.environ.get("EMAIL_FROM")
    if not api_key or not from_addr:
        print("RESEND_API_KEY or EMAIL_FROM not set; skipping email.", file=sys.stderr)
        return False
    try:
        r = httpx.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": [to], "subject": subject, "html": html_body},
            timeout=30,
        )
    except httpx.HTTPError as e:
        # Timeout / connect failure must not crash the digest run — the
        # digest file is already written; only the email leg failed.
        print(f"resend failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    if r.status_code >= 400:
        print(f"resend failed: {r.status_code} {r.text}", file=sys.stderr)
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--since", help="explicit start date (YYYY-MM-DD); overrides --hours")
    ap.add_argument("--email", help="send via Resend to this address")
    ap.add_argument("--dry-run", action="store_true", help="don't file or email")
    args = ap.parse_args()

    pc_links: dict[str, str] = {}
    eps = collect_episodes(hours=args.hours, since=args.since, links_out=pc_links)
    if not eps:
        print(f"No played episodes in the last {args.hours}h.")
        return
    print(f"found {len(eps)} episodes in window")

    # Map EpisodePages back to their wiki paths so we can compute bridges
    # and render with listened-state badges.
    from podmind.frontmatter import read_raw_dir
    from podmind.threads import bridges_in_window, format_threads_md
    from podmind.threads_llm import synthesize_threads
    by_raw_dir: dict[str, Path] = {}
    for p in (WIKI_DIR / "episodes").glob("*.md"):
        rd = read_raw_dir(p)
        if rd:
            by_raw_dir[rd] = p
    pairs: list[tuple[Path, EpisodePage]] = []
    for ep in eps:
        if ep.raw_dir and ep.raw_dir in by_raw_dir:
            pairs.append((by_raw_dir[ep.raw_dir], ep))
    print(f"  {len(pairs)} episodes have wiki pages")

    person_br, topic_br = bridges_in_window([p for p, _ in pairs])
    print(f"  bridges: {len(person_br)} people, {len(topic_br)} topics")

    eps_with_slugs = [(p.stem, ep) for p, ep in pairs]
    print(f"synthesizing threads via configured LLM provider...")
    threads, uncat = synthesize_threads(
        eps_with_slugs, person_br, topic_br,
    )
    print(f"  → {len(threads)} threads + {len(uncat)} uncategorized")

    date_str = (args.since or datetime.now().strftime("%Y-%m-%d"))
    if args.since:
        window_label = f"since {args.since}"
    elif args.hours != 24:
        window_label = f"the last {args.hours}h"
    else:
        window_label = "the last 24h"
    lookup = {p.stem: ep for p, ep in pairs}
    content = format_threads_md(
        threads, uncat,
        date_str=date_str,
        window_label=window_label,
        episode_lookup=lookup,
    )
    # Safety net: strip backticks around [[wiki-links]] in case any leaked
    # through the prompt path (same defense as the legacy synthesize()).
    content = re.sub(r"`(\[\[[^\]]+\]\])`", r"\1", content)
    if not args.dry_run:
        out = file_digest(date_str, content, len(eps), args.hours)
        print(f"→ filed {out}")

    if args.email and not args.dry_run:
        # lazy: keeps cron/CLI startup light (same pattern as threads/frontmatter above)
        from podmind.digest_email import render_email_html
        subject = f"Threads — {date_str}"
        if threads:
            top = max(threads, key=lambda t: len(t.episode_slugs))
            n = len(top.episode_slugs)
            subject = f"{top.name} · {n} episode{'s' if n != 1 else ''} ({date_str})"
        html_body = render_email_html(
            threads, uncat,
            date_str=date_str,
            window_label=window_label,
            episode_lookup=lookup,
            vault_name=os.environ.get("OBSIDIAN_VAULT", "podcast-wiki"),
            source_overrides=pc_links,
        )
        ok = send_email(args.email, subject, html_body)
        if ok:
            print(f"→ emailed to {args.email}")

    print("\n--- digest ---\n")
    print(content)


if __name__ == "__main__":
    main()
