#!/usr/bin/env -S uv run python
"""retry_none_sample — re-cascade a sampled batch of `transcript_source: "none"` episodes.

The 8,525 `"none"` verdicts accumulated while `mlx_whisper` was missing from
the uv environment after the podmind rename. `try_whisper()` returned False on
ImportError, then the cascade stamped `"none"` per the give-up rule in
`transcribe_one`. Now that mlx_whisper is reinstalled, these episodes are
re-tryable — but first we measure the recovery rate on a sample before
mass-resetting.

Usage:
    ./bin/retry_none_sample.py --sample 100         # default: 100, --only-played
    ./bin/retry_none_sample.py --sample 50 --no-whisper  # cheap tiers only
    ./bin/retry_none_sample.py --sample 100 --reset-only # reset to null, no cascade
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from podmind import paths
from podmind.curation import is_music_channel
from podmind.transcript import transcribe_one


def collect_none_candidates(only_played: bool, skip_music: bool) -> list[Path]:
    cands: list[Path] = []
    for m in paths.EPISODES_DIR.glob("*/*/meta.json"):
        try:
            d = json.loads(m.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("transcript_source") != "none":
            continue
        # "none" is overloaded: it also marks quarantined yt-dlp duplicates
        # and corrupt transcripts. Those were deliberately dropped — never
        # resurrect them (a reset would re-transcribe a dup, then tick_prep
        # would re-quarantine it: a pointless churn loop).
        if d.get("dropped_reason") or d.get("transcript_corrupt_reason"):
            continue
        if only_played and not (d.get("listened") or (d.get("played_up_to") or 0) > 0):
            continue
        if skip_music and is_music_channel(m.parent.parent.name):
            continue
        cands.append(m.parent)
    return cands


def reset_to_null(epdir: Path) -> None:
    """Flip transcript_source from 'none' back to None so the cascade picks it up."""
    meta_path = epdir / "meta.json"
    d = json.loads(meta_path.read_text())
    d["transcript_source"] = None
    from podmind.jsonio import write_json_atomic
    write_json_atomic(meta_path, d)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=100, help="how many to test (random)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-whisper", action="store_true", help="skip whisper tier")
    ap.add_argument("--all-episodes", action="store_true", help="don't filter to only-played")
    ap.add_argument("--include-music", action="store_true", help="include yt-music channels")
    ap.add_argument("--reset-only", action="store_true", help="reset to null and exit (no cascade)")
    args = ap.parse_args()

    only_played = not args.all_episodes
    skip_music = not args.include_music

    print(f"Collecting candidates (only_played={only_played}, skip_music={skip_music})...")
    cands = collect_none_candidates(only_played, skip_music)
    print(f"  {len(cands)} 'none' episodes eligible")

    random.seed(args.seed)
    chosen = random.sample(cands, min(args.sample, len(cands)))
    print(f"  sampled {len(chosen)} (seed={args.seed})")

    print("Resetting transcript_source to null on sample...")
    for epdir in chosen:
        reset_to_null(epdir)

    if args.reset_only:
        print(f"Done. {len(chosen)} episodes reset to null. Re-run cascade to retry.")
        return

    print(f"Running cascade (allow_whisper={not args.no_whisper})...")
    outcomes: Counter[str] = Counter()
    for i, epdir in enumerate(chosen, 1):
        outcome = transcribe_one(epdir, allow_whisper=not args.no_whisper)
        outcomes[outcome] += 1
        if i % 10 == 0 or i == len(chosen):
            print(f"  {i}/{len(chosen)}  {dict(outcomes)}")

    print(f"\nFinal: {dict(outcomes)}")
    recovered = sum(v for k, v in outcomes.items() if k not in ("none", "skipped"))
    print(f"Recovery rate: {recovered}/{len(chosen)} = {100*recovered/len(chosen):.1f}%")


if __name__ == "__main__":
    main()
