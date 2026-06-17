#!/usr/bin/env -S uv run python
"""tick_prep — find next N pending listened episodes, quarantine yt-dlp dups, emit dispatch table.

Usage: ./bin/tick_prep.py [N]   (default 12)

Output: a Python dict literal printed to stdout — copy into the agent dispatch loop.
Side effects: byte-identical transcripts among pending get transcript_source=none + dropped_reason
in their meta.json (yt-dlp ytsearch1 dedup).
"""
import hashlib
import json
import sys
from pathlib import Path

from podmind.paths import DATA_ROOT as ROOT
SONNET_THRESHOLD = 150_000  # bytes; >= → Sonnet, < → Haiku


def transcript_hash(p: Path) -> str | None:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _guid_of(meta: dict) -> str | None:
    """Canonical content-identity key: explicit guid (RSS) or yt:<video_id>."""
    g = meta.get("guid")
    if g:
        return g
    vid = meta.get("youtube_video_id")
    return f"yt:{vid}" if vid else None


def load_ingested() -> tuple[set[str], dict[str, str]]:
    """Return (set of ingested raw_dirs, guid → ingested raw_dir)."""
    from podmind.frontmatter import read_raw_dir
    rds: set[str] = set()
    guids: dict[str, str] = {}
    for f in (ROOT / "wiki/episodes").glob("*.md"):
        rd = read_raw_dir(f)
        if not rd:
            continue
        rds.add(rd)
        meta_path = ROOT / "raw/episodes" / rd / "meta.json"
        try:
            m = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if g := _guid_of(m):
            guids.setdefault(g, rd)
    return rds, guids


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=12)
    ap.add_argument("--only", help="comma-separated raw_dirs to restrict to")
    args = ap.parse_args()
    n = args.n
    only = set(args.only.split(",")) if args.only else None
    ingested, ingested_guids = load_ingested()
    candidates = []
    for m in (ROOT / "raw/episodes").glob("*/*/meta.json"):
        rd = f"{m.parent.parent.name}/{m.parent.name}"
        if only is not None and rd not in only:
            continue
        if rd in ingested:
            continue
        try:
            d = json.loads(m.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not (d.get("listened") or (d.get("played_up_to") or 0) > 0):
            continue
        if d.get("transcript_source") in (None, "none"):
            continue
        date = (d.get("pub_date") or d.get("watched_at") or "0000-00-00")[:10]
        tpath = m.parent / "transcript.md"
        size = tpath.stat().st_size if tpath.exists() else 0
        candidates.append((date, rd, size, tpath, m, _guid_of(d)))
    candidates.sort(key=lambda x: x[0], reverse=True)

    # Two-pass dedup: (1) by guid/video_id against already-ingested + within-batch
    # (catches YouTube re-watches whose transcripts drift byte-wise but reference
    # the same video); (2) by transcript byte-hash (catches yt-dlp ytsearch1 mismatches
    # where different videos share a transcript).
    seen_guids: dict[str, tuple[str, str]] = {f"existing:{g}": ("", rd) for g, rd in ingested_guids.items()}
    seen_hash: dict[str, tuple[str, str]] = {}
    quarantined: list[tuple[str, str, str]] = []  # (rd, ref_rd, reason)
    kept: list[tuple[str, str, int]] = []

    def _quarantine(mpath: Path, rd: str, ref_rd: str, reason: str) -> None:
        from podmind.jsonio import write_json_atomic
        d = json.loads(mpath.read_text())
        d["transcript_source"] = "none"
        d["dropped_reason"] = reason
        write_json_atomic(mpath, d)
        quarantined.append((rd, ref_rd, reason))

    for date, rd, size, tpath, mpath, guid in candidates:
        if size == 0:
            continue
        # Guid dedup
        if guid:
            existing_key = f"existing:{guid}"
            if existing_key in seen_guids:
                ref_rd = seen_guids[existing_key][1]
                _quarantine(mpath, rd, ref_rd, f"duplicate guid {guid} of already-ingested {ref_rd}")
                continue
            if guid in seen_guids:
                ref_rd = seen_guids[guid][1]
                _quarantine(mpath, rd, ref_rd, f"duplicate guid {guid} (same as pending {ref_rd})")
                continue
            seen_guids[guid] = (date, rd)
        # Transcript-hash dedup
        h = transcript_hash(tpath)
        if not h:
            continue
        if h in seen_hash:
            ref_rd = seen_hash[h][1]
            _quarantine(mpath, rd, ref_rd, f"duplicate transcript (byte-identical to {ref_rd}); yt-dlp ytsearch1 dup")
            continue
        seen_hash[h] = (date, rd)
        kept.append((date, rd, size))

    top = kept if only is not None else kept[:n]
    out = {
        "pending_total": len(kept),
        "quarantined_this_run": [{"rd": rd, "dup_of": ref, "reason": reason} for rd, ref, reason in quarantined],
        "dispatch": [
            {
                "idx": f"{i+1:02d}",
                "raw_dir": rd,
                "date": date,
                "size": size,
                "model": "sonnet" if size >= SONNET_THRESHOLD else "haiku",
            }
            for i, (date, rd, size) in enumerate(top)
        ],
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
