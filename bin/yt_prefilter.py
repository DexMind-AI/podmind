#!/usr/bin/env -S uv run python
"""yt_prefilter — quarantine YT episodes not worth transcribing.

Rules:
1. Drop all `-topic` channels (YouTube Music auto-generated; no spoken content)
2. Drop channels in EXCLUDE_CHANNELS (loaded from the vault config file
   $PODMIND_DATA_ROOT/config/curation.json → {"exclude_channels": [...]})
3. Keep everything else, including single-watch channels (may surface interesting one-offs)

Quarantining sets `transcript_source = "none"` and adds `dropped_reason` so the
transcript cascade and whisper job both skip them.

Usage: ./bin/yt_prefilter.py [--dry-run]
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from _lib import EPISODES_DIR
from podmind.curation import is_music_channel, load_exclude_channels

EXCLUDE_CHANNELS = load_exclude_channels()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = ap.parse_args()

    chan_count: Counter[str] = Counter()
    metas: list[tuple[Path, str, dict]] = []
    for m in EPISODES_DIR.glob("yt-*/*/meta.json"):
        chan = m.parent.parent.name
        try:
            d = json.loads(m.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        chan_count[chan] += 1
        metas.append((m, chan, d))

    drop_music = 0
    drop_excluded = 0
    keep = 0
    skip_already = 0
    for m, chan, d in metas:
        if d.get("transcript_source") is not None:
            skip_already += 1
            continue
        reason = None
        if is_music_channel(chan):
            reason = "music channel (Topic / Vevo — no spoken content)"
        elif chan in EXCLUDE_CHANNELS:
            reason = f"{chan.removeprefix('yt-')} channel excluded by user policy"
        if reason is None:
            keep += 1
            continue
        if not args.dry_run:
            d["transcript_source"] = "none"
            d["dropped_reason"] = f"yt prefilter: {reason}"
            from podmind.jsonio import write_json_atomic
            write_json_atomic(m, d)
        if "music" in reason:
            drop_music += 1
        else:
            drop_excluded += 1

    action = "would drop" if args.dry_run else "dropped"
    print(f"channels: {len(chan_count)}")
    print(f"yt episodes total: {len(metas)}")
    print(f"  already classified (skip):    {skip_already}")
    print(f"  {action} (music channels):    {drop_music}")
    print(f"  {action} (excluded channels): {drop_excluded}")
    print(f"  kept for transcription:       {keep}")


if __name__ == "__main__":
    main()
