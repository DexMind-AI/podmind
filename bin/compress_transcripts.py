#!/usr/bin/env -S uv run python
"""compress_transcripts — migrate raw VTT transcripts to/from xz compression.

Compresses every ``raw/episodes/**/transcript.vtt`` to ``transcript.vtt.xz``
(lzma), removing the plain file. Idempotent and per-file atomic. The plain-text
``transcript.md`` is never touched.

Usage:
  ./bin/compress_transcripts.py [--dry-run]      # compress (default)
  ./bin/compress_transcripts.py --decompress     # reverse (.xz → plain .vtt)
"""
import argparse

from _lib import EPISODES_DIR
from podmind import transcripts


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--decompress", action="store_true",
                    help="reverse: rewrite .xz transcripts as plain .vtt")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args()

    count, delta = transcripts.migrate_tree(
        EPISODES_DIR, decompress=args.decompress, dry_run=args.dry_run)

    if args.decompress:
        verb = "would decompress" if args.dry_run else "decompressed"
        print(f"{verb} {count} transcript(s)")
    else:
        verb = "would compress" if args.dry_run else "compressed"
        label = "projected" if args.dry_run else "reclaimed"
        print(f"{verb} {count} transcript(s); {label}: {delta / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
