"""Pull fresh YouTube watch history via yt-dlp + browser cookies.

Designed to run under launchd / daily.sh, NOT inside an interactive Claude
Code session (where reading browser cookies is blocked as a credential read).

Reads /feed/history with the user's signed-in Chrome cookies. For each watch
that matches a Pocket Casts subscription channel and isn't already on disk,
materializes a `raw/episodes/yt-<channel-slug>/<watched-date>-<title-slug>/meta.json`.
The transcript cascade then picks these up on its next run.

Output: count of new dirs created. Appends a `## [date] youtube-history` log entry.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from podmind import paths, youtube

console = Console()


def fetch_history(browser: str = "chrome", limit: int | None = None) -> list[dict[str, Any]]:
    """Run yt-dlp to print recent watch-history entries as one JSON-line each."""
    if not shutil.which("yt-dlp"):
        raise SystemExit("yt-dlp not found in PATH; install with `uv tool install yt-dlp`")

    # We use --flat-playlist for reliability: per-video format resolution breaks
    # the whole batch on the first deleted/restricted/format-unavailable video
    # in the history (ads, music, dead links). Channel info will be "NA" for
    # most entries, but we then resolve channels in a second pass below.
    # Duration comes back populated in flat-playlist mode for :ythistory
    # (verified 2026-05-13). That lets the curation filter drop shorts and
    # under-5min clips without a per-video probe — keeping daily cron fast.
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", browser,
        "--no-warnings",
        "--ignore-errors",
        "--flat-playlist",
        "--print", "%(id)s|%(title)s|%(channel)s|%(upload_date)s|%(duration)s",
        ":ythistory",
    ]
    if limit:
        cmd.extend(["--playlist-end", str(limit)])

    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, timeout=300)
    except subprocess.CalledProcessError as e:
        # yt-dlp commonly fails on a few entries but still prints most; only abort on total failure.
        if not e.output:
            console.print(f"[red]yt-dlp failed:[/] {e.stderr.strip()[:500]}")
            return []
        out = e.output
    except subprocess.TimeoutExpired:
        console.print("[red]yt-dlp timed out[/]")
        return []

    entries: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 4)
        if len(parts) < 3:
            continue
        vid, title, channel = parts[0], parts[1], parts[2]
        upload_date = parts[3] if len(parts) > 3 else ""
        duration_raw = parts[4] if len(parts) > 4 else ""
        if not vid or vid == "NA":
            continue
        # Synthesise a watched_at from upload_date when available; else "today".
        if upload_date and upload_date != "NA":
            watched_iso = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
        else:
            watched_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            duration_sec = int(float(duration_raw)) if duration_raw and duration_raw != "NA" else 0
        except ValueError:
            duration_sec = 0
        entries.append({
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": title,
            "channel": channel if channel and channel != "NA" else "",
            "watched_at": watched_iso,
            "duration_sec": duration_sec,
        })
    return entries


def resolve_channels(entries: list[dict[str, Any]], browser: str = "chrome") -> int:
    """Second-pass enrichment for entries that came back as 'NA' from flat-
    playlist mode. Fills channel, duration, and category in one yt-dlp probe.

    Important: this call does NOT pass --cookies-from-browser. With cookies,
    yt-dlp gets an authenticated player response from YouTube that demands
    specific (often DRM/member-only) formats and aborts with "Requested format
    not available". Without cookies it gets the public anonymous response
    that always has standard formats. Channel/duration/category are public
    metadata so no auth is needed for this lookup.

    Also fills duration_sec=0 entries while we're already probing — these are
    the music mixes / unresolvable items where the duration filter alone would
    leak. Returns count of entries that got channel filled.
    """
    needs = [e for e in entries if not e.get("channel") or not e.get("duration_sec")]
    if not needs:
        return 0
    resolved = 0
    for e in needs:
        try:
            cmd = [
                "yt-dlp",
                "--no-warnings", "--skip-download",
                "--print", "%(channel)s|%(duration)s|%(categories)s",
                f"https://www.youtube.com/watch?v={e['video_id']}",
            ]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=30).strip()
            line = out.splitlines()[0] if out else ""
            parts = line.split("|", 2)
            channel = parts[0] if len(parts) > 0 else ""
            duration_raw = parts[1] if len(parts) > 1 else ""
            categories = parts[2] if len(parts) > 2 else ""
            if channel and channel != "NA" and not e.get("channel"):
                e["channel"] = channel
                resolved += 1
            if duration_raw and duration_raw != "NA":
                try:
                    e["duration_sec"] = int(float(duration_raw))
                except ValueError:
                    pass
            # Music category check — yt-dlp returns categories as a Python-list
            # repr like "['Music']" in flat output. Substring match is enough.
            if "Music" in categories:
                e["_music_category"] = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return resolved


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="podmind.youtube_history")
    p.add_argument("--browser", default="chrome",
                   help="browser to read cookies from (chrome, safari, firefox, brave, edge)")
    # 500 is well above YouTube's typical continuation cap (~280-500 entries)
    # so we get the whole visible history in one pull. Materialize() dedups
    # against existing raw dirs, so the steady-state cost is dominated by the
    # flat-playlist pull (~30s) regardless of how many of those are new.
    p.add_argument("--limit", type=int, default=500,
                   help="cap entries fetched from history (most-recent first); ~0.1s/entry in flat mode")
    p.add_argument("--pc-channel-match-only", action="store_true",
                   help="legacy: ingest only watches whose channel matches a current PC subscription. "
                        "Default policy is now broad: keep everything that isn't a short or music "
                        "(see podmind.curation.should_exclude_yt).")
    args = p.parse_args(argv)

    console.rule("[bold]YouTube history pull")
    raw = fetch_history(browser=args.browser, limit=args.limit)
    console.print(f"fetched [bold]{len(raw)}[/] watch-history entries from {args.browser}")
    if not raw:
        return 0

    n_resolved = resolve_channels(raw, browser=args.browser)
    console.print(f"channel-resolved {n_resolved}/{sum(1 for e in raw if not e.get('channel')) + n_resolved} entries that flat-mode returned as NA")

    raw_pre_filter = len(raw)
    music_cat_dropped = sum(1 for e in raw if e.get("_music_category"))
    raw = [e for e in raw if not e.get("_music_category")]
    if music_cat_dropped:
        console.print(f"dropped [bold]{music_cat_dropped}[/] entries flagged as YouTube category 'Music' during probe")

    entries = [
        youtube.WatchEntry(
            video_id=e["video_id"], url=e["url"], title=e["title"],
            channel=e["channel"], watched_at=e["watched_at"],
            duration_sec=e.get("duration_sec", 0),
        )
        for e in raw
    ]

    if args.pc_channel_match_only:
        subs = youtube.load_pc_subscriptions()
        matchers = youtube.podcast_channel_matchers(subs)
        before = len(entries)
        entries = [e for e in entries if youtube.is_podcast_match(e, matchers)]
        console.print(f"[yellow]pc-channel-match-only:[/] {before} → [bold]{len(entries)}[/]")

    stats = youtube.materialize(entries)
    console.print(stats)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (
        f"\n## [{ts}] youtube-history\n"
        f"- source: yt-dlp :ythistory ({args.browser} cookies)\n"
        f"- entries fetched: {raw_pre_filter}\n"
        f"- new episode dirs: {stats['new']}\n"
        f"- skipped (already present): {stats['skipped_existing']}\n"
        f"- filtered: short={stats.get('skipped_short', 0)} "
        f"music_channel={stats.get('skipped_music_channel', 0)} "
        f"music_category={music_cat_dropped} "
        f"under_5min={stats.get('skipped_under_5min', 0)}\n"
        f"- policy: {'pc-channel-match-only' if args.pc_channel_match_only else 'broad (curation.should_exclude_yt)'}\n"
    )
    with paths.LOG_FILE.open("a") as f:
        f.write(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
