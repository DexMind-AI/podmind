"""Transcript cascade: try RSS → publisher → podcast-index → YouTube → whisper.

Each tier writes:
    transcript.vtt.xz  (compressed VTT; plain transcript.vtt when
                        PODMIND_COMPRESS_TRANSCRIPTS=0 — see podmind.transcripts)
    transcript.md      (always; plain text, one paragraph per cue)
and updates meta.json's transcript_source field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import httpx
from rich.console import Console

from podmind import paths, secrets, transcripts

console = Console()

VTT_CUE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ")


def _read_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def _write_meta(d: Path, meta: dict) -> None:
    """Atomic write — see podmind.jsonio for why."""
    from podmind.jsonio import write_json_atomic
    write_json_atomic(d / "meta.json", meta)


def _vtt_to_plaintext(vtt: str) -> str:
    """Strip cue headers and timestamps; collapse to running text."""
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith("NOTE"):
            continue
        if VTT_CUE_RE.match(line):
            continue
        if line.isdigit():
            continue
        lines.append(line)
    return "\n".join(lines)


def _srt_to_vtt(srt: str) -> str:
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt)
    return "WEBVTT\n\n" + body


# ---------- Tier 1: RSS Podcasting 2.0 transcript tag ----------

def try_rss(d: Path, meta: dict) -> bool:
    feed_url = meta.get("show_feed_url")
    guid = meta.get("guid")
    if not feed_url or not guid:
        return False
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        console.print(f"[yellow]rss parse fail[/] {feed_url}: {e}")
        return False

    target = None
    for entry in parsed.entries:
        if entry.get("id") == guid or entry.get("guid") == guid:
            target = entry
            break
    if target is None:
        # Pocket Casts UUID often != RSS guid; fall back to title match.
        title = meta["title"].strip().lower()
        for entry in parsed.entries:
            if entry.get("title", "").strip().lower() == title:
                target = entry
                break
    if target is None:
        return False

    transcript_links = target.get("podcast_transcript") or []
    if isinstance(transcript_links, dict):
        transcript_links = [transcript_links]
    # Prefer VTT > SRT > anything.
    def rank(t: dict) -> int:
        ttype = (t.get("type") or "").lower()
        if "vtt" in ttype: return 0
        if "srt" in ttype: return 1
        return 2
    transcript_links = sorted(transcript_links, key=rank)

    for t in transcript_links:
        url = t.get("url")
        if not url:
            continue
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError:
            continue
        text = r.text
        ttype = (t.get("type") or "").lower()
        if "srt" in ttype or text.lstrip().startswith("1\n"):
            text = _srt_to_vtt(text)
        if "WEBVTT" in text or text.startswith("WEBVTT"):
            transcripts.write_vtt(d, text)
            (d / "transcript.md").write_text(_vtt_to_plaintext(text))
        else:
            # Some shows publish HTML/JSON transcripts; treat as plain text.
            (d / "transcript.md").write_text(text)
        meta["transcript_source"] = "rss"
        _write_meta(d, meta)
        return True
    return False


# ---------- Tier 1.5: Publisher website scraping ----------

def _strip_html(html: str) -> str:
    """Quick-and-dirty HTML → plain text."""
    class _T(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self._skip = 0
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript"):
                self._skip += 1
            elif tag in ("p", "br", "div", "li"):
                self.parts.append("\n")
        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript") and self._skip:
                self._skip -= 1
            elif tag == "p":
                self.parts.append("\n")
        def handle_data(self, data):
            if not self._skip:
                self.parts.append(data)
    p = _T()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _ezra_klein_scrape(meta: dict) -> str | None:
    """NYT publishes Ezra Klein transcripts at predictable URLs.

    Try the search endpoint first — too brittle to guess slugs.
    """
    title = meta.get("title", "")
    pub_date = meta.get("pub_date", "")
    if not title or not pub_date:
        return None
    try:
        # NYT search has stable JSON-ish endpoints but they require auth/cookies.
        # Public path: scrape the search results page.
        q = httpx.QueryParams({
            "query": f"transcript {title}",
            "sort": "best",
        })
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0 podcast-wiki/0.1"}, timeout=20.0, follow_redirects=True) as c:
            r = c.get(f"https://www.nytimes.com/search?{q}")
            if r.status_code != 200:
                return None
            # Look for first link to a /podcasts/transcript-... page
            m = re.search(r'href="(/\d{4}/\d{2}/\d{2}/podcasts/[^"]*transcript[^"]*\.html)"', r.text)
            if not m:
                return None
            transcript_url = "https://www.nytimes.com" + m.group(1)
            r2 = c.get(transcript_url)
            if r2.status_code != 200:
                return None
            text = _strip_html(r2.text)
            # Sanity: must be substantial
            if len(text) < 5000:
                return None
            return text
    except (httpx.HTTPError, OSError):
        return None


def _thefp_scrape(meta: dict) -> str | None:
    """The Free Press posts some transcripts at thefp.com/p/<slug>."""
    title = meta.get("title", "")
    if not title:
        return None
    try:
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0 podcast-wiki/0.1"}, timeout=20.0, follow_redirects=True) as c:
            # TFP runs on Substack — use their search.
            r = c.get("https://www.thefp.com/api/v1/archive", params={
                "search": title[:80], "limit": 5,
            })
            if r.status_code != 200:
                return None
            try:
                data = r.json()
            except json.JSONDecodeError:
                return None
            posts = data if isinstance(data, list) else data.get("posts", [])
            if not posts:
                return None
            # Take first hit, fetch body.
            slug = posts[0].get("canonical_url") or f"https://www.thefp.com/p/{posts[0].get('slug','')}"
            r2 = c.get(slug)
            if r2.status_code != 200:
                return None
            text = _strip_html(r2.text)
            if "transcript" not in text.lower()[:2000] or len(text) < 5000:
                return None
            return text
    except (httpx.HTTPError, OSError):
        return None


PUBLISHER_SCRAPERS = {
    "The Ezra Klein Show": _ezra_klein_scrape,
    "Honestly with Bari Weiss": _thefp_scrape,
    "The Free Press": _thefp_scrape,
}


def try_publisher(d: Path, meta: dict) -> bool:
    show = meta.get("show", "")
    fn = PUBLISHER_SCRAPERS.get(show)
    if fn is None:
        return False
    text = fn(meta)
    if not text:
        return False
    (d / "transcript.md").write_text(text)
    meta["transcript_source"] = "publisher"
    _write_meta(d, meta)
    return True


# ---------- Tier 1.7: Podcast Index API ----------

def try_podcast_index(d: Path, meta: dict) -> bool:
    """Look up the episode in podcastindex.org and grab any transcript URL.

    Requires `podcast_index_key` and `podcast_index_secret` in secrets.
    Sign up free at https://api.podcastindex.org/.
    """
    s = secrets.load()
    key = s.get("podcast_index_key")
    sec = s.get("podcast_index_secret")
    if not key or not sec:
        return False
    guid = meta.get("guid", "")
    title = meta.get("title", "")
    feed_url = meta.get("show_feed_url", "")
    if not (guid or (title and feed_url)):
        return False

    show = meta.get("show", "")

    def _hdrs() -> dict:
        epoch = str(int(time.time()))
        return {
            "User-Agent": "podcast-wiki/0.1",
            "X-Auth-Date": epoch,
            "X-Auth-Key": key,
            "Authorization": hashlib.sha1((key + sec + epoch).encode()).hexdigest(),
        }

    try:
        with httpx.Client(timeout=20.0) as c:
            ep = None
            # Strategy 1: GUID + feedurl (works only when feed URLs match between us and PI).
            if guid and feed_url:
                r = c.get("https://api.podcastindex.org/api/1.0/episodes/byguid",
                          params={"guid": guid, "feedurl": feed_url}, headers=_hdrs())
                if r.status_code == 200:
                    ep = r.json().get("episode")
            # Strategy 2: find canonical PI feed, then list recent episodes → match title.
            feed_id = None
            if feed_url:
                r = c.get("https://api.podcastindex.org/api/1.0/podcasts/byfeedurl",
                          params={"url": feed_url}, headers=_hdrs())
                if r.status_code == 200:
                    f = r.json().get("feed")
                    if f and f.get("id"):
                        feed_id = f["id"]
            if not feed_id and show:
                r = c.get("https://api.podcastindex.org/api/1.0/search/byterm",
                          params={"q": show}, headers=_hdrs())
                if r.status_code != 200:
                    return False
                feeds = r.json().get("feeds", [])
                # Prefer exact title match.
                show_n = show.strip().lower()
                exact = [f for f in feeds if (f.get("title") or "").strip().lower() == show_n]
                feeds = exact or feeds
                if not feeds:
                    return False
                feed_id = feeds[0]["id"]
            # Strategy 2 episode lookup. NOTE: this block sat UNREACHABLE
            # below a `return False` from (likely) an indentation accident,
            # which silently disabled the byfeedid path — the podcast-index
            # tier only ever worked via the byguid direct hit. Found in the
            # 2026-06-12 code review; restored.
            if ep is None:
                if not feed_id or not title:
                    return False
                r2 = c.get("https://api.podcastindex.org/api/1.0/episodes/byfeedid",
                           params={"id": feed_id, "max": 100}, headers=_hdrs())
                if r2.status_code != 200:
                    return False
                items = r2.json().get("items", [])
                needle = title.strip().lower()
                # Exact-match first, then prefix-match.
                for it in items:
                    if (it.get("title") or "").strip().lower() == needle:
                        ep = it
                        break
                if not ep:
                    for it in items:
                        if (it.get("title") or "").strip().lower().startswith(needle[:60]):
                            ep = it
                            break
            if not ep:
                return False
            transcript_links = ep.get("transcripts") or []
            if not transcript_links:
                return False
            # Prefer VTT > SRT > anything.
            transcript_links = sorted(transcript_links, key=lambda t: 0 if "vtt" in (t.get("type") or "").lower() else 1 if "srt" in (t.get("type") or "").lower() else 2)
            for t in transcript_links:
                url = t.get("url")
                if not url:
                    continue
                # Transcript URLs are not Podcast Index endpoints — fetch without auth headers.
                r = httpx.get(url, follow_redirects=True, timeout=30.0,
                              headers={"User-Agent": "podcast-wiki/0.1"})
                if r.status_code != 200:
                    continue
                text = r.text
                ttype = (t.get("type") or "").lower()
                if "srt" in ttype or text.lstrip().startswith("1\n"):
                    text = _srt_to_vtt(text)
                if "WEBVTT" in text or text.startswith("WEBVTT"):
                    transcripts.write_vtt(d, text)
                    (d / "transcript.md").write_text(_vtt_to_plaintext(text))
                else:
                    (d / "transcript.md").write_text(text)
                meta["transcript_source"] = "podcast-index"
                _write_meta(d, meta)
                return True
    except (httpx.HTTPError, OSError, json.JSONDecodeError):
        return False
    return False


# ---------- Tier 2: YouTube auto-captions via yt-dlp ----------

def try_youtube(d: Path, meta: dict) -> bool:
    if not shutil.which("yt-dlp"):
        return False
    target = meta.get("youtube_url") or f"ytsearch1:{meta['show']} {meta['title']}"
    out = d / "transcript"
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-sub",          # uploader-provided subtitles (higher quality)
        "--write-auto-sub",     # YouTube auto-captions (fallback)
        "--sub-lang", "en,en-US,en-GB,de",
        "--convert-subs", "vtt",
        "--no-warnings",
        "-o", str(out),
        target,
    ]
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    # yt-dlp may exit non-zero if a secondary lang (e.g. de) hits HTTP 429,
    # but the primary en VTT often landed before the failure — so don't gate
    # on returncode; gate on whether a usable VTT actually exists.
    vtt_files = [v for v in d.glob("transcript*.vtt") if v.stat().st_size > 100]
    if not vtt_files:
        return False
    # Prefer English subs when multiple langs landed.
    vtt_files.sort(key=lambda v: (0 if "en" in v.name else 1, v.name))
    vtt = vtt_files[0]
    text = vtt.read_text()
    (d / "transcript.md").write_text(_vtt_to_plaintext(text))
    # Remove yt-dlp's raw VTT files (possibly language-suffixed); write_vtt
    # re-creates the canonical transcript.vtt(.xz) from the text we hold.
    for stray in d.glob("transcript*.vtt"):
        stray.unlink(missing_ok=True)
    transcripts.write_vtt(d, text)
    meta["transcript_source"] = "youtube"
    _write_meta(d, meta)
    return True


# ---------- Tier 3: local mlx-whisper ----------

def try_whisper(d: Path, meta: dict) -> bool:
    audio_url = meta.get("audio_url")
    if not audio_url:
        return False
    try:
        import mlx_whisper  # noqa: F401
    except ImportError as e:
        # This used to be a silent skip → return False, which made the
        # cascade stamp `transcript_source: "none"` (a give-up verdict).
        # That accumulated 8525 false-"none" episodes after the podmind
        # rename dropped the whisper extra from the data-folder venv
        # (2026-05-13). Now we raise so the cascade aborts loudly: the
        # caller can decide between `--no-whisper` (legitimate skip) and
        # silently corrupting state.
        raise RuntimeError(
            "mlx_whisper missing — refusing to mark episodes as 'none' "
            "while the whisper tier is unavailable. Fix: cd $PODMIND_DATA_ROOT "
            "&& uv sync --extra whisper. Or pass --no-whisper to skip this tier "
            "explicitly (won't stamp 'none')."
        ) from e

    paths.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(httpx.URL(audio_url).path).suffix or ".mp3"
    audio_path = paths.AUDIO_DIR / f"{d.parent.name}-{d.name}{ext}"
    if not audio_path.exists() or audio_path.stat().st_size < 1024:
        try:
            with httpx.stream("GET", audio_url, follow_redirects=True, timeout=300) as r:
                r.raise_for_status()
                with audio_path.open("wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            console.print(f"[yellow]audio download fail[/] {audio_url}: {e}")
            return False

    try:
        from mlx_whisper import transcribe as mlx_transcribe
        result = mlx_transcribe(
            str(audio_path),
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        )
        # Build VTT manually from segments.
        vtt = ["WEBVTT", ""]
        for seg in result.get("segments", []):
            start = _fmt_ts(seg["start"])
            end = _fmt_ts(seg["end"])
            vtt.append(f"{start} --> {end}")
            vtt.append(seg["text"].strip())
            vtt.append("")
        transcripts.write_vtt(d, "\n".join(vtt))
        (d / "transcript.md").write_text(result.get("text", "").strip())
        meta["transcript_source"] = "whisper"
        _write_meta(d, meta)
        return True
    except Exception as e:
        # mlx_whisper can raise on corrupt audio (ffmpeg decode errors), missing
        # model weights, OOM, or torchaudio backend issues. Without this catch,
        # ONE bad mp3 crashes the cascade and launchd's KeepAlive restarts it on
        # the same episode forever — observed 2026-05-13→14 when a 0-byte/
        # header-missing mp3 reached the top of the date-desc queue. We return
        # False so the orchestrator stamps `transcript_source: "none"` (legitimate
        # give-up for THIS episode) and the loop advances to the next candidate.
        console.print(f"[red]whisper failed[/] {d.parent.name}/{d.name}: {type(e).__name__}: {str(e)[:200]}")
        # Delete the audio — it's either corrupt or our env can't handle it; no
        # point keeping it on disk and re-failing every batch.
        audio_path.unlink(missing_ok=True)
        return False
    finally:
        # Keep audio cached only on success; on failure (including the except
        # branch above) the audio is removed there. This finally block runs on
        # success path for cleanup.
        if meta.get("transcript_source") == "whisper":
            audio_path.unlink(missing_ok=True)


def _fmt_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ---------- Orchestration ----------

def transcribe_one(d: Path, *, allow_whisper: bool = True, whisper_only: bool = False) -> str:
    meta = _read_meta(d)
    if meta.get("transcript_source"):
        return meta["transcript_source"]
    if not whisper_only:
        if try_rss(d, meta):
            return "rss"
        if try_publisher(d, meta):
            return "publisher"
        if try_podcast_index(d, meta):
            return "podcast-index"
        if try_youtube(d, meta):
            return "youtube"
    if allow_whisper and try_whisper(d, meta):
        return "whisper"
    # Only stamp "none" (give-up) when whisper was actually attempted.
    # Otherwise leave transcript_source as None so a later whisper run can pick it up,
    # and so a concurrent whisper job isn't disrupted by a parallel cheap-tiers cascade.
    if allow_whisper:
        meta["transcript_source"] = "none"
        _write_meta(d, meta)
        return "none"
    return "skipped"


def on_ac_power() -> bool:
    """True if the Mac is on AC. Returns True on non-macOS (no gating)."""
    if sys.platform != "darwin":
        return True
    try:
        out = subprocess.check_output(["pmset", "-g", "ps"], text=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return True  # fail open — don't block work because pmset hiccuped
    return "AC Power" in out.splitlines()[0] if out else True


def user_idle_seconds() -> float:
    """Seconds since the user last touched keyboard / mouse / trackpad (macOS).

    Reads `HIDIdleTime` from `ioreg -c IOHIDSystem`, which is in nanoseconds.
    Returns +inf on non-macOS or if ioreg hiccups (fail open: don't block work
    just because we can't tell whether the user is around).

    Use case: gate the whisper batch on user-idleness so the multi-GB model
    isn't loaded while the user is actively typing. The 4-min in-flight
    episode is unaffected; only the *next* episode is skipped when the user
    has been active recently.
    """
    if sys.platform != "darwin":
        return float("inf")
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"], text=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return float("inf")  # fail open
    # HIDIdleTime appears once per HID device; the smallest value reflects the
    # most-recent input (across keyboard, trackpad, mouse, etc.).
    nanos: list[int] = []
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                nanos.append(int(line.split("=")[-1].strip()))
            except ValueError:
                continue
    if not nanos:
        return float("inf")
    return min(nanos) / 1_000_000_000


def transcribe_all(
    *,
    allow_whisper: bool = True,
    whisper_only: bool = False,
    limit: int | None = None,
    only_played: bool = False,
    require_ac: bool = False,
    require_user_idle_sec: float = 0.0,
) -> dict[str, int]:
    counts = {"rss": 0, "publisher": 0, "podcast-index": 0, "youtube": 0, "whisper": 0, "none": 0, "skipped": 0, "aborted-battery": 0, "aborted-user-active": 0}
    # Read meta once and sort by pub_date desc so freshly-listened episodes get
    # transcribed first. Fall back to watched_at (yt-*) and finally to filesystem
    # mtime so the sort key is total. Episodes already at non-null transcript_source
    # are filtered out HERE so they don't eat the --limit budget; without this
    # filter, the date-desc sort + limit traps the loop on the freshest 50
    # episodes (which the daily cascade has already transcribed) and the whisper
    # backlog never drains.
    candidates: list[tuple[str, Path, dict]] = []
    for mfile in paths.EPISODES_DIR.glob("*/*/meta.json"):
        try:
            m = json.loads(mfile.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if m.get("transcript_source"):
            continue
        if only_played and not (m.get("listened") or (m.get("played_up_to") or 0) > 0):
            continue
        # Sort key: ingested_at (when the watch actually surfaced — set by
        # youtube materialize) beats pub_date, because yt watched_at/pub_date
        # are synthesized from the video's UPLOAD date for dir-name stability;
        # an old video watched yesterday would otherwise sort to the bottom.
        date = (m.get("ingested_at") or m.get("pub_date") or m.get("watched_at") or "")[:10] or "0000-00-00"
        candidates.append((date, mfile, m))
    candidates.sort(key=lambda t: t[0], reverse=True)
    if limit:
        candidates = candidates[:limit]
    for date, mfile, meta in candidates:
        if require_ac and not on_ac_power():
            counts["aborted-battery"] += 1
            console.print("[yellow]on battery — aborting batch[/]")
            break
        if require_user_idle_sec > 0:
            idle = user_idle_seconds()
            if idle < require_user_idle_sec:
                counts["aborted-user-active"] += 1
                console.print(
                    f"[yellow]user active ({idle:.0f}s idle, need ≥{require_user_idle_sec:.0f}s) "
                    f"— aborting batch[/]"
                )
                break
        # Defensive: dir may have been deleted between candidate-list build and now
        # (e.g. concurrent music cleanup). Skip rather than crash the batch.
        if not mfile.exists():
            counts["vanished"] = counts.get("vanished", 0) + 1
            continue
        try:
            result = transcribe_one(mfile.parent, allow_whisper=allow_whisper, whisper_only=whisper_only)
        except FileNotFoundError:
            counts["vanished"] = counts.get("vanished", 0) + 1
            continue
        except RuntimeError as e:
            # e.g. mlx_whisper missing (try_whisper raises rather than
            # silently stamping "none"). Abort the batch LOUDLY but cleanly:
            # a raise here would crash the process, and under launchd
            # KeepAlive that's a 60s crash-restart loop. The caller (and
            # whisper.sh, via the ABORTED_ERROR sentinel) backs off instead.
            counts["aborted-error"] = counts.get("aborted-error", 0) + 1
            console.print(f"[red]batch aborted:[/] {e}")
            break
        counts[result] = counts.get(result, 0) + 1
        console.print(f"  {result:8} {mfile.parent.parent.name}/{mfile.parent.name}")
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="podmind.transcript")
    p.add_argument("--no-whisper", action="store_true", help="skip the whisper fallback")
    p.add_argument("--whisper-only", action="store_true", help="skip cheap tiers; run whisper only on episodes where transcript_source is null")
    p.add_argument("--limit", type=int, help="cap episodes processed")
    p.add_argument("--episode", help="path to a single episode dir; transcribe just that one")
    p.add_argument("--only-played", action="store_true", help="only episodes with played_up_to>0 or listened=true")
    p.add_argument("--require-ac", action="store_true", help="abort the batch as soon as the Mac switches to battery power (macOS only)")
    p.add_argument("--require-user-idle-sec", type=float, default=0.0,
                   help="abort the batch when the user has had keyboard/mouse input within the last N seconds (macOS only). 0 disables. 300 = pause whisper for 5 min after last input.")
    args = p.parse_args(argv)

    if args.episode:
        d = Path(args.episode).resolve()
        result = transcribe_one(d, allow_whisper=not args.no_whisper, whisper_only=args.whisper_only)
        console.print(f"{result}: {d}")
        return 0
    counts = transcribe_all(
        allow_whisper=not args.no_whisper,
        whisper_only=args.whisper_only,
        limit=args.limit,
        only_played=args.only_played,
        require_ac=args.require_ac,
        require_user_idle_sec=args.require_user_idle_sec,
    )
    console.print(counts)
    # Machine-readable sentinels for shell callers (whisper.sh). rich's
    # console.print pretty-prints the dict across multiple lines in a
    # non-tty (width 80), which made whisper.sh's single-line dict grep
    # never match — its idle-sleep never fired and it re-spawned Python
    # in a tight loop (the "fork failed" incidents). Plain print, one line.
    print(f"COUNTS_JSON: {json.dumps(counts)}", flush=True)
    progress_keys = ("rss", "publisher", "podcast-index", "youtube", "whisper", "none")
    abort_keys = ("aborted-battery", "aborted-user-active", "aborted-error")
    empty = all(counts.get(k, 0) == 0 for k in progress_keys + abort_keys)
    print(f"BACKLOG_EMPTY={'1' if empty else '0'}", flush=True)
    if counts.get("aborted-error", 0):
        print("ABORTED_ERROR=1", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
