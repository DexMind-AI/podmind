#!/usr/bin/env python
"""Generate AI-assisted topic merge proposals.

This script is intentionally thin in this checkout: the manual proposal parser
and apply path live in podmind.proposals and merge_topic_dups.py. Keeping the
shared PROPOSALS_PATH here lets podmind curate coordinate the two-step merge
flow without hard-coding vault paths in multiple places.
"""
from __future__ import annotations

import argparse

from _lib import WIKI_DIR

PROPOSALS_PATH = WIKI_DIR.parent / "reports" / "merge-topic-proposals.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prepare topic merge proposals")
    ap.add_argument("--recent", type=int, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.parse_args(argv)
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PROPOSALS_PATH.exists():
        PROPOSALS_PATH.write_text(
            "# Topic merge proposals\n\n"
            "No AI-generated proposals are available in this environment.\n"
        )
    print(PROPOSALS_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
