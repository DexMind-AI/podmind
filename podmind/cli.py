"""podmind — single CLI front door.

Thin router: each subcommand forwards its remaining arguments verbatim to the
underlying bin script (``python bin/x.py``) or module (``python -m podmind.x``)
and returns its exit code. podmind is run as a source/editable install (a uv
project), so ``bin/`` and ``scripts/`` are present at the repo root.

The router never re-declares a script's flags — ``podmind ingest --help`` forwards
``--help`` so the script prints its own usage.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_ROOT = Path(__file__).resolve().parent.parent


class _Verb(NamedTuple):
    kind: str       # "bin" | "py" | "sh"
    target: str
    description: str


_VERBS: dict[str, _Verb] = {
    "ingest": _Verb("bin", "ingest_run.py",
                    "summarize pending episodes into the wiki (prep → LLM → finalize)"),
    "query": _Verb("bin", "query.py",
                   "semantic query over the wiki, filed back as a synthesis page"),
    "lint": _Verb("bin", "lint.py",
                  "wiki health pass: near-dup topics, broken links, stale badges"),
    "curate": _Verb("bin", "curate.py",
                    "wiki health & curation: lint, enrich, merge, nightly"),
    "sync": _Verb("py", "podmind.sync",
                  "pull Pocket Casts state and run the transcript cascade"),
    "transcript": _Verb("py", "podmind.transcript",
                        "run the transcript cascade on pending episodes"),
    "digest": _Verb("bin", "daily_digest.py",
                    "a short digest of what you listened to recently"),
    "stats": _Verb("bin", "build_stats.py",
                   "regenerate wiki/stats.md and the analytics charts"),
    "embed": _Verb("bin", "embed_all.py",
                   "build or refresh the semantic-search embedding cache"),
    "compress-transcripts": _Verb("bin", "compress_transcripts.py",
                                  "migrate raw VTT transcripts to/from xz compression"),
    "refresh-badges": _Verb("py", "podmind.refresh_badges",
                            "re-derive listened-state badges on wiki pages"),
    "demo": _Verb("sh", "scripts/demo.sh",
                  "zero-key demo: real pipeline over 3 bundled synthetic episodes"),
}


def _build_command(verb: str, rest: list[str]) -> list[str]:
    v = _VERBS[verb]
    if v.kind == "py":
        return [sys.executable, "-m", v.target, *rest]
    if v.kind == "bin":
        return [sys.executable, str(_ROOT / "bin" / v.target), *rest]
    return [str(_ROOT / v.target), *rest]  # sh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="podmind",
        description="LLM-built knowledge wiki of your podcast + YouTube listening.",
    )
    sub = parser.add_subparsers(dest="verb", metavar="<command>")
    for verb, v in _VERBS.items():
        sub.add_parser(verb, help=v.description, add_help=False)

    # Pre-scan: if a known verb appears after other args, suggest the right order
    # before argparse rejects the leading token as an invalid subcommand.
    effective = list(argv) if argv is not None else sys.argv[1:]
    if effective and effective[0] not in _VERBS and effective[0] not in ("-h", "--help"):
        misplaced = next((a for a in effective[1:] if a in _VERBS), None)
        if misplaced:
            parser.error(f"the command must come first — try: podmind {misplaced} ...")

    args, rest = parser.parse_known_args(argv)
    if not args.verb:
        parser.print_help()
        return 1
    try:
        return subprocess.run(_build_command(args.verb, rest), check=False).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
