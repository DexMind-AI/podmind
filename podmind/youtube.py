"""Ingest YouTube watch history into raw/episodes/.

Source: Google Takeout → YouTube and YouTube Music → history.
Two formats supported:
    - watch-history.json (cleanest; select JSON when filing the Takeout request)
    - watch-history.html (Takeout default)

Each watched video becomes its own episode dir under
    raw/episodes/yt-<channel-slug>/<yyyy-mm-dd>-<title-slug>/meta.json

`meta.json` schema is parallel to the Pocket Casts variant so the rest of the
pipeline (transcript cascade, ingest) treats them uniformly. Extra fields:
    youtube_url, youtube_video_id, source: "youtube"

By default only entries whose channel matches a current Pocket Casts
subscription (by author or title) are ingested. Pass `--include-all` to take
every watched video — that includes music, tutorials, and short clips, so
expect noise.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from rich.console import Console

from podmind import paths

console = Console()

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")


@dataclass
class WatchEntry:
    video_id: str
    url: str
    title: str
    channel: str
    watched_at: str  # ISO timestamp
    duration_sec: int = 0  # 0 = unknown (Takeout HTML/JSON don't include it)


# ---------- Parsers ----------

def _video_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def parse_json(path: Path) -> list[WatchEntry]:
    data = json.loads(path.read_text())
    out: list[WatchEntry] = []
    for row in data:
        url = row.get("titleUrl") or ""
        vid = _video_id(url)
        if not vid:
            continue
        title = (row.get("title") or "").removeprefix("Watched ").strip()
        channel = ""
        subs = row.get("subtitles") or []
        if subs and isinstance(subs, list):
            channel = (subs[0].get("name") or "").strip()
        out.append(WatchEntry(
            video_id=vid,
            url=url,
            title=title,
            channel=channel,
            watched_at=row.get("time") or "",
        ))
    return out


class _TakeoutHTMLParser(HTMLParser):
    """Walk the Takeout watch-history.html `outer-cell` blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[WatchEntry] = []
        self._depth = 0
        self._in_content_cell = False
        self._cell_div_depth = 0
        self._buffer: list[str] = []
        self._links: list[tuple[str, str]] = []  # (href, text)
        self._current_text: list[str] = []
        self._current_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag == "div" and "content-cell" in (attrs_d.get("class") or "") and "mdl-typography--body-1" in (attrs_d.get("class") or ""):
            self._in_content_cell = True
            self._cell_div_depth = 1
            self._buffer = []
            self._links = []
        elif self._in_content_cell:
            if tag == "div":
                self._cell_div_depth += 1
            if tag == "a":
                self._current_href = attrs_d.get("href") or ""
                self._current_text = []
            elif tag == "br":
                self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_content_cell:
            return
        if tag == "a" and self._current_href is not None:
            text = "".join(self._current_text).strip()
            self._links.append((self._current_href, text))
            self._buffer.append(text)
            self._current_href = None
            self._current_text = []
        elif tag == "div":
            self._cell_div_depth -= 1
            if self._cell_div_depth == 0:
                self._flush_cell()
                self._in_content_cell = False

    def handle_data(self, data: str) -> None:
        if not self._in_content_cell:
            return
        if self._current_href is not None:
            self._current_text.append(data)
        else:
            self._buffer.append(data)

    def _flush_cell(self) -> None:
        if not self._links:
            return
        video_url, video_title = self._links[0]
        vid = _video_id(video_url)
        if not vid:
            return
        channel = self._links[1][1] if len(self._links) > 1 else ""
        # Last text line in the cell is the timestamp.
        text = "".join(self._buffer)
        time_line = ""
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line and any(ch.isdigit() for ch in line):
                time_line = line
                break
        self.entries.append(WatchEntry(
            video_id=vid,
            url=video_url,
            title=video_title.strip(),
            channel=channel.strip(),
            watched_at=time_line,
        ))


def parse_html(path: Path) -> list[WatchEntry]:
    p = _TakeoutHTMLParser()
    p.feed(path.read_text(errors="replace"))
    return p.entries


def parse_takeout(path: Path) -> list[WatchEntry]:
    if path.suffix.lower() == ".json":
        return parse_json(path)
    if path.suffix.lower() in {".html", ".htm"}:
        return parse_html(path)
    raise SystemExit(f"unknown takeout format: {path.suffix}")


# ---------- Filtering ----------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def load_pc_subscriptions() -> list[dict[str, Any]]:
    f = paths.FEEDS_DIR / "subscriptions.json"
    if not f.exists():
        return []
    return json.loads(f.read_text()).get("podcasts", [])


def podcast_channel_matchers(subs: list[dict[str, Any]]) -> set[str]:
    """Channel-name strings to match against YouTube subtitles[0].name."""
    out: set[str] = set()
    for s in subs:
        for key in ("title", "author"):
            v = s.get(key)
            if v:
                out.add(_norm(v))
    return out


def is_podcast_match(entry: WatchEntry, matchers: set[str]) -> bool:
    ch = _norm(entry.channel)
    if not ch:
        return False
    if ch in matchers:
        return True
    # Soft match: channel contains a subscription name or vice versa.
    # Both sides must be ≥4 chars to avoid 2-letter channels (AK, LP, …)
    # accidentally matching long titles via substring.
    if len(ch) < 5:
        return False
    for m in matchers:
        if len(m) < 5:
            continue
        if m in ch or ch in m:
            return True
    return False


# ---------- Materialization ----------

def _watched_iso(s: str) -> tuple[str, str]:
    """Return (yyyy-mm-dd, full_iso) best-effort."""
    if not s:
        return ("0000-00-00", "")
    # JSON time looks like "2026-04-22T18:43:11.000Z"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt.date().isoformat(), dt.isoformat())
    except ValueError:
        pass
    # HTML format: "Apr 22, 2026, 6:43:11 PM CET"
    for fmt in (
        "%b %d, %Y, %I:%M:%S %p %Z",
        "%b %d, %Y, %I:%M:%S %p %Z",
        "%b %d, %Y, %I:%M:%S %p",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt.date().isoformat(), dt.replace(tzinfo=UTC).isoformat())
        except ValueError:
            continue
    return ("0000-00-00", s)


def _meta_for(entry: WatchEntry) -> dict[str, Any]:
    pub_date, watched_iso = _watched_iso(entry.watched_at)
    return {
        "show": entry.channel or "(unknown channel)",
        "show_uuid": None,
        "show_feed_url": None,
        "title": entry.title,
        "guid": f"yt:{entry.video_id}",
        "pub_date": pub_date,
        "published_iso": watched_iso,
        "duration_sec": entry.duration_sec,  # 0 if unknown; refined by yt-dlp on transcribe
        "audio_url": entry.url,
        "youtube_url": entry.url,
        "youtube_video_id": entry.video_id,
        "listened": True,  # YouTube history = watched
        "played_up_to": 0,
        "playing_status": 3,
        "transcript_source": None,
        "source": "youtube",
        "watched_at": watched_iso,
        # When this dir was first materialized — i.e. when the watch actually
        # surfaced in history. watched_at is synthesized from upload_date for
        # dir-name stability across daily re-pulls, so for an old video
        # watched today it's YEARS in the past; ingested_at is the only field
        # that reflects "freshly watched". transcribe_all sorts on it so new
        # watches of old videos get transcribed first, not last.
        "ingested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def materialize(entries: list[WatchEntry]) -> dict[str, int]:
    """Write a meta.json for each kept entry under raw/episodes/yt-<channel>/.

    Applies the YT curation policy (curation.should_exclude_yt): drops shorts,
    music auto-channels, and anything KNOWN to be <5min. Duration-unknown
    entries (Takeout, missing flat-playlist data) pass through — we prefer
    over-inclusion to silent loss.
    """
    from podmind.curation import should_exclude_yt

    counts = {
        "new": 0,
        "updated": 0,
        "skipped_existing": 0,
        "skipped_short": 0,
        "skipped_music": 0,
        "skipped_under_5min": 0,
    }
    seen: set[tuple[str, str, str]] = set()
    for e in entries:
        if not e.title or not e.video_id:
            continue
        exclude, reason = should_exclude_yt(e.channel or "", e.duration_sec, e.url)
        if exclude:
            key = "skipped_" + reason.replace("-", "_")
            counts[key] = counts.get(key, 0) + 1
            continue
        meta = _meta_for(e)
        show = "yt-" + (e.channel or "unknown")
        d = paths.episode_dir(show, meta["pub_date"], e.title)
        key = (show, meta["pub_date"], e.title)
        if key in seen:
            continue
        seen.add(key)
        d.mkdir(parents=True, exist_ok=True)
        mfile = d / "meta.json"
        if mfile.exists():
            counts["skipped_existing"] += 1
            continue
        mfile.write_text(json.dumps(meta, indent=2))
        counts["new"] += 1
    return counts


# ---------- CLI ----------

def cmd_ingest(args: argparse.Namespace) -> None:
    path = Path(args.path).expanduser().resolve()
    console.rule("[bold]YouTube Takeout ingest")
    entries = parse_takeout(path)
    console.print(f"parsed [bold]{len(entries)}[/] watch entries from {path.name}")

    if not args.include_all:
        subs = load_pc_subscriptions()
        matchers = podcast_channel_matchers(subs)
        before = len(entries)
        entries = [e for e in entries if is_podcast_match(e, matchers)]
        console.print(f"podcast-channel filter: {before} → [bold]{len(entries)}[/] entries")
        if matchers:
            console.print(f"  matched against {len(matchers)} PC channel/author names")

    if args.limit:
        entries = entries[: args.limit]

    stats = materialize(entries)
    console.print(stats)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (
        f"\n## [{ts}] youtube-ingest\n"
        f"- source: {path.name}\n"
        f"- entries seen: {len(entries)}\n"
        f"- new episode dirs: {stats['new']}\n"
        f"- skipped (already present): {stats['skipped_existing']}\n"
        f"- mode: {'all' if args.include_all else 'pc-channel-match'}\n"
    )
    with paths.LOG_FILE.open("a") as f:
        f.write(block)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="podmind.youtube")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="ingest a Takeout watch-history file")
    pi.add_argument("path", help="path to watch-history.json or watch-history.html")
    pi.add_argument("--include-all", action="store_true",
                    help="ingest every watched video, not just those matching a PC subscription")
    pi.add_argument("--limit", type=int, help="cap entries (for testing)")
    pi.set_defaults(func=cmd_ingest)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
