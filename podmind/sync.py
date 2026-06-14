"""Top-level sync: Pocket Casts → transcripts → log entry."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from rich.console import Console

from podmind import paths, pocketcasts, transcript

console = Console()


def append_log(stats_pc: dict, stats_tx: dict) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = [
        f"\n## [{ts}] sync",
        f"- subscriptions: {stats_pc['shows']}",
        f"- new episodes: {stats_pc['new']}",
        f"- listened-state changes: {stats_pc['state_updated']}",
        f"- transcripts (rss/youtube/whisper/none/skipped):"
        f" {stats_tx.get('rss',0)}/{stats_tx.get('youtube',0)}/{stats_tx.get('whisper',0)}/"
        f"{stats_tx.get('none',0)}/{stats_tx.get('skipped',0)}",
        "",
    ]
    with paths.LOG_FILE.open("a") as f:
        f.write("\n".join(block))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="podmind.sync")
    p.add_argument("--no-whisper", action="store_true")
    p.add_argument("--no-transcribe", action="store_true", help="metadata only")
    p.add_argument("--limit", type=int)
    p.add_argument("--only-played", action="store_true",
                   help="transcribe only episodes the user has played (in-progress or finished)")
    args = p.parse_args(argv)

    console.rule("[bold]Pocket Casts sync")
    stats_pc = pocketcasts.sync_all()
    console.print(stats_pc)

    if args.no_transcribe:
        stats_tx = {}
    else:
        console.rule("[bold]Transcript cascade")
        stats_tx = transcript.transcribe_all(
            allow_whisper=not args.no_whisper,
            limit=args.limit,
            only_played=args.only_played,
        )
        console.print(stats_tx)

    append_log(stats_pc, stats_tx)
    console.rule("[green]done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
