"""Single source of truth for content-curation predicates.

Used by both `podmind.youtube.materialize` (ingest-time filter) and the
`bin/` curation scripts (`yt_prefilter`, `build_stats`, `sensitivity_audit`).
Keep this in sync when curation rules evolve.
"""
from __future__ import annotations

import json

from podmind import paths

# Minimum duration (seconds) for a YouTube video to be worth ingesting.
# 5 minutes is the rough "shorts + clips + music videos" cutoff: typical
# Shorts are <90s, viral clips <3min, music videos 3-5min, real podcast/
# talk content >5min. This is the cheapest single filter that approximates
# the user's "≥50% watched, except shorts and music" policy without needing
# a per-video API probe (duration comes free from yt-dlp flat-playlist).
MIN_YT_DURATION_SEC = 300


def is_music_channel(name: str) -> bool:
    """Match YouTube Music auto-channels (the '- Topic' suffix pattern) and
    Vevo channels — both are audio-only, no spoken content worth transcribing.

    Accepts either the raw channel display name (`"Lana Del Rey - Topic"`) or
    the slugged form (`"yt-lana-del-rey-topic"` or `"lana-del-rey-topic"`).
    Case-insensitive.
    """
    if not name:
        return False
    n = name.lower().strip()
    return (
        n.endswith(" - topic")
        or n.endswith("-topic")
        or "vevo" in n
    )


def load_exclude_channels() -> set[str]:
    """User-curated channel blocklist, kept in the vault (not the repo):
    $PODMIND_DATA_ROOT/config/curation.json → {"exclude_channels": [...]}."""
    cfg = paths.DATA_ROOT / "config" / "curation.json"
    if not cfg.exists():
        return set()
    return set(json.loads(cfg.read_text()).get("exclude_channels", []))


def should_exclude_yt(channel: str, duration_sec: int, url: str = "") -> tuple[bool, str]:
    """Decide whether to drop a YouTube watch-history entry from ingest.

    Returns (exclude, reason). `reason` is "" when kept, otherwise a short
    tag for log aggregation: 'short', 'music-channel', 'under-5min'.

    Policy (replaces the older PC-subscription channel filter):
    - keep everything watched, regardless of subscription status
    - drop Shorts (URL contains /shorts/)
    - drop music auto-channels (-topic, vevo)
    - drop anything KNOWN to be <5 minutes (duration_sec > 0 and < 300)
      Unknown duration (0) passes through — better to over-include than
      silently lose a watch, transcript cascade is cheap.
    """
    if url and "/shorts/" in url:
        return True, "short"
    if is_music_channel(channel):
        return True, "music-channel"
    if 0 < duration_sec < MIN_YT_DURATION_SEC:
        return True, "under-5min"
    return False, ""
