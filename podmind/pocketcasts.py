"""Unofficial Pocket Casts web API client.

Endpoints used (reverse-engineered from the web client; subject to change):
    POST  https://api.pocketcasts.com/user/login
    POST  https://api.pocketcasts.com/user/podcast/list
    POST  https://api.pocketcasts.com/user/podcast/episodes
    POST  https://api.pocketcasts.com/user/history    (in-progress + listened state)
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx
from rich.console import Console
from rich.progress import track

from podmind import paths, secrets

API = "https://api.pocketcasts.com"
console = Console()


def _client(token: str | None = None) -> httpx.Client:
    headers = {"User-Agent": "podcast-wiki/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=headers, timeout=30.0)


def login(email: str, password: str) -> str:
    with _client() as c:
        r = c.post("/user/login", json={"email": email, "password": password, "scope": "webplayer"})
        r.raise_for_status()
        token = r.json()["token"]
    secrets.update(pocketcasts_token=token, pocketcasts_email=email)
    return token


def _token() -> str:
    s = secrets.load()
    tok = s.get("pocketcasts_token")
    if not tok:
        raise SystemExit("No Pocket Casts token. Run: uv run python -m podmind.pocketcasts login")
    return tok


def list_subscriptions() -> list[dict[str, Any]]:
    with _client(_token()) as c:
        r = c.post("/user/podcast/list", json={"v": 1})
        r.raise_for_status()
        return r.json().get("podcasts", [])


def list_episodes(podcast_uuid: str) -> list[dict[str, Any]]:
    """User-state for every episode in a podcast (uuid + playedUpTo + status).

    Note: this endpoint does NOT include episode titles — only state. To match
    these back to RSS episodes we cross-reference titles via the public
    /mobile/podcast/full endpoint (see `public_podcast_titles`).
    """
    with _client(_token()) as c:
        r = c.post("/user/podcast/episodes", json={"uuid": podcast_uuid})
        r.raise_for_status()
        return r.json().get("episodes", [])


def public_podcast_titles(podcast_uuid: str) -> dict[str, str]:
    """Map of episode_uuid → title for every episode in a podcast.

    Uses the unauthenticated /mobile/podcast/full endpoint (the same one PC's
    mobile app uses to render show pages). Returns titles for ALL episodes,
    archived or not — the bridge we need to recover listened state for
    episodes that have rolled off the 100-cap /user/history window.
    """
    url = f"https://podcast-api.pocketcasts.com/mobile/podcast/full/{podcast_uuid}"
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError:
        return {}
    data = r.json()
    eps = (data.get("podcast") or data).get("episodes") or []
    return {
        e["uuid"]: (e.get("title") or "").strip()
        for e in eps
        if e.get("uuid")
    }


def listening_history() -> list[dict[str, Any]]:
    """Returns episodes the user has interacted with: listened or in-progress."""
    with _client(_token()) as c:
        r = c.post("/user/history", json={})
        r.raise_for_status()
        return r.json().get("episodes", [])


FEED_URL_CACHE = paths.FEEDS_DIR / "feed_urls.json"


def _load_feed_url_cache() -> dict[str, str]:
    if FEED_URL_CACHE.exists():
        return json.loads(FEED_URL_CACHE.read_text())
    return {}


def _save_feed_url_cache(cache: dict[str, str]) -> None:
    paths.FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    FEED_URL_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _resolve_feed_url(show: dict[str, Any]) -> str | None:
    """Resolve a podcast's RSS feed URL via iTunes Search.

    PC's subscription `url` is the show's website, not the feed. iTunes Search
    is well-documented and unauthenticated. Match by title + author.
    """
    title = show.get("title") or ""
    author = (show.get("author") or "").strip().lower()
    params = {"term": title, "media": "podcast", "limit": 10}
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": "podcast-wiki/0.1"}) as c:
            r = c.get("https://itunes.apple.com/search", params=params)
            r.raise_for_status()
            results = r.json().get("results", [])
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    if not results:
        return None
    if author:
        for hit in results:
            if (hit.get("artistName") or "").strip().lower() == author and hit.get("feedUrl"):
                return hit["feedUrl"]
    for hit in results:
        if (hit.get("collectionName") or "").strip().lower() == title.strip().lower() and hit.get("feedUrl"):
            return hit["feedUrl"]
    return results[0].get("feedUrl")


def _fetch_feed(url: str) -> list[dict[str, Any]]:
    """Fetch an RSS feed and return normalized episode entries (newest first)."""
    if not url:
        return []
    with httpx.Client(headers={"User-Agent": "podcast-wiki/0.1"}, timeout=30.0) as c:
        r = c.get(url, follow_redirects=True)
        r.raise_for_status()
        body = r.content
    parsed = feedparser.parse(body)
    out: list[dict[str, Any]] = []
    for e in parsed.entries:
        guid = e.get("id") or e.get("guid")
        if not guid:
            continue
        published_iso = e.get("published") or e.get("updated") or ""
        pub_date = ""
        pp = e.get("published_parsed") or e.get("updated_parsed")
        if pp:
            pub_date = f"{pp.tm_year:04d}-{pp.tm_mon:02d}-{pp.tm_mday:02d}"
        elif published_iso:
            pub_date = published_iso[:10]
        if not pub_date:
            continue
        encs = e.get("enclosures") or []
        audio_url = encs[0].get("href") if encs else None
        dur = e.get("itunes_duration")
        duration_sec = 0
        if isinstance(dur, str) and dur:
            parts = dur.split(":")
            try:
                if len(parts) == 3:
                    duration_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif len(parts) == 2:
                    duration_sec = int(parts[0]) * 60 + int(parts[1])
                else:
                    duration_sec = int(parts[0])
            except ValueError:
                duration_sec = 0
        out.append({
            "uuid": guid,
            "title": e.get("title") or "(untitled)",
            "published": published_iso,
            "pub_date": pub_date,
            "duration": duration_sec,
            "url": audio_url,
        })
    return out


def _meta_for(show: dict[str, Any], ep: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    duration = ep.get("duration", 0) or 0
    played = state.get("playedUpTo", 0) or 0
    # playingStatus: 1 unplayed, 2 in-progress, 3 played
    status = state.get("playingStatus", 1)
    listened = status == 3 or (duration > 0 and played >= 0.95 * duration)
    return {
        "show": show["title"],
        "show_uuid": show["uuid"],
        "show_feed_url": show.get("url"),
        "title": ep["title"],
        "guid": ep["uuid"],
        "pub_date": ep["pub_date"],
        "published_iso": ep["published"],
        "duration_sec": duration,
        "audio_url": ep.get("url"),
        "listened": listened,
        "played_up_to": played,
        "playing_status": status,
        "transcript_source": None,
    }


def sync_all() -> dict[str, int]:
    """Sync every subscribed podcast's episode list and listened-state.

    Idempotent: only refreshes listened/played fields on existing meta.json;
    creates new episode dirs for first-seen episodes.
    """
    subs = list_subscriptions()
    paths.FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    (paths.FEEDS_DIR / "subscriptions.json").write_text(
        json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "podcasts": subs}, indent=2)
    )
    console.print(f"[bold]{len(subs)}[/] subscribed podcasts")

    raw_history = listening_history()
    history = {h["uuid"]: h for h in raw_history}
    history_by_show_title = {
        (h.get("podcastUuid"), (h.get("title") or "").strip().lower()): h
        for h in raw_history
        if h.get("podcastUuid") and h.get("title")
    }
    console.print(f"[bold]{len(raw_history)}[/] episodes in listening history")

    feed_url_cache = _load_feed_url_cache()
    resolved_count = 0

    new_count = updated_count = 0
    for show in track(subs, description="Syncing shows"):
        feed_url = feed_url_cache.get(show["uuid"])
        if not feed_url:
            feed_url = _resolve_feed_url(show)
            if feed_url:
                feed_url_cache[show["uuid"]] = feed_url
                resolved_count += 1
            else:
                console.print(f"[yellow]warn[/] no feed URL for {show['title']}")
                continue
        try:
            pc_eps = list_episodes(show["uuid"])
            pc_state = {ep["uuid"]: ep for ep in pc_eps}
            # /user/podcast/episodes returns state but no titles. Bridge via
            # the public /mobile/podcast/full endpoint (uuid → title), so we
            # can fall back to PC state when /user/history (capped at 100
            # globally) hasn't surfaced an episode.
            uuid_to_title = public_podcast_titles(show["uuid"])
            pc_state_by_title = {
                title.lower(): pc_state[uuid]
                for uuid, title in uuid_to_title.items()
                if uuid in pc_state and title
            }
            feed_eps = _fetch_feed(feed_url)
        except httpx.HTTPError as e:
            console.print(f"[yellow]warn[/] {show['title']}: {e}")
            continue
        for ep in feed_eps:
            # Match RSS guid → PC uuid via (show_uuid, title) since they differ.
            h = history_by_show_title.get(
                (show["uuid"], ep["title"].strip().lower())
            ) or {}
            pc_ep = (
                pc_state.get(h.get("uuid", ""), {}) if h else {}
            ) or pc_state_by_title.get(ep["title"].strip().lower(), {})
            state = {
                "playedUpTo": h.get("playedUpTo") or pc_ep.get("playedUpTo", 0),
                "playingStatus": h.get("playingStatus") or pc_ep.get("playingStatus", 1),
            }
            ep["duration"] = ep["duration"] or pc_ep.get("duration", 0)
            meta = _meta_for(show, ep, state)
            meta["show_feed_url"] = feed_url
            d = paths.episode_dir(show["title"], meta["pub_date"], ep["title"])
            d.mkdir(parents=True, exist_ok=True)
            mfile = d / "meta.json"
            if mfile.exists():
                old = json.loads(mfile.read_text())
                # Preserve transcript_source from previous run.
                meta["transcript_source"] = old.get("transcript_source")
                if (
                    old.get("listened") == meta["listened"]
                    and old.get("played_up_to") == meta["played_up_to"]
                ):
                    continue
                updated_count += 1
            else:
                new_count += 1
            mfile.write_text(json.dumps(meta, indent=2))
    _save_feed_url_cache(feed_url_cache)
    return {
        "shows": len(subs),
        "new": new_count,
        "state_updated": updated_count,
        "feed_urls_resolved": resolved_count,
    }


def cmd_login(args: argparse.Namespace) -> None:
    email = args.email or input("Pocket Casts email: ")
    password = args.password or getpass.getpass("Pocket Casts password: ")
    login(email, password)
    console.print("[green]✓[/] token saved")


def cmd_sync(_args: argparse.Namespace) -> None:
    stats = sync_all()
    console.print(stats)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="podmind.pocketcasts")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login")
    pl.add_argument("--email")
    pl.add_argument("--password")
    pl.set_defaults(func=cmd_login)

    ps = sub.add_parser("sync")
    ps.set_defaults(func=cmd_sync)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
