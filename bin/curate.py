#!/usr/bin/env python
"""podmind curate — wiki health & curation umbrella.

Subcommands forward to the existing standalone scripts; `nightly` runs the full
unattended routine. The vault is a git repo, so the destructive steps (frontmatter
repair, topic merge) only run after a successful **git checkpoint** commit — a
restore point a bad merge can be undone against with `git reset --hard <sha>`.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "bin"))
from _lib import WIKI_DIR  # noqa: E402

DATA_ROOT = WIKI_DIR.parent

# simple subcommand -> bin script (args forwarded verbatim)
_FORWARDS = {
    "lint": "lint.py",
    "enrich": "enrich_cross_links.py",
    "repair-frontmatter": "repair_frontmatter.py",
    "repair-links": "repair_episode_links.py",
}


def _run(script: str, *args: str) -> int:
    return subprocess.run(
        [sys.executable, str(_ROOT / "bin" / script), *args]
    ).returncode


def git_checkpoint(label: str) -> str:
    """Commit the current vault state as a restore point; return the commit sha.

    Raises CalledProcessError if git add/commit fails, so nightly can abort the
    destructive steps when there is no recoverable checkpoint. ``--allow-empty``
    means a clean tree still yields a usable restore-point sha.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    subprocess.run(["git", "-C", str(DATA_ROOT), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(DATA_ROOT), "commit", "--allow-empty", "-q",
         "-m", f"curate {label} — {stamp}"],
        check=True,
    )
    out = subprocess.run(
        ["git", "-C", str(DATA_ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _merge_ai_apply(recent: int) -> int:
    from merge_topic_ai import PROPOSALS_PATH

    rc = _run("merge_topic_ai.py", "--recent", str(recent))
    if rc != 0:
        return rc
    return _run("merge_topic_dups.py", "--apply-from", str(PROPOSALS_PATH))


def _cmd_merge(rest: list[str]) -> int:
    if "--ai" in rest:
        rest = [a for a in rest if a != "--ai"]
        rc = _run("merge_topic_ai.py", *rest)
        if rc != 0 or "--apply" not in rest:
            return rc
        from merge_topic_ai import PROPOSALS_PATH

        return _run("merge_topic_dups.py", "--apply-from", str(PROPOSALS_PATH))
    return _run("merge_topic_dups.py", *rest)


def nightly(recent: int) -> int:
    failed: list[str] = []

    # additive — safe regardless of version control state
    if _run("enrich_cross_links.py", "--recent", str(recent)) != 0:
        failed.append("enrich")

    # restore point BEFORE the destructive steps — a commit capturing tonight's
    # ingest + enrich, so a bad merge reverts to it while keeping the additive work.
    have_restore = False
    try:
        sha = git_checkpoint("pre-merge")
        print(f"[curate] restore point {sha[:10]}")
        have_restore = True
    except Exception as e:  # git failure must not crash the routine
        print(f"[curate] git checkpoint FAILED: {e}", file=sys.stderr)
        failed.append("git-checkpoint")

    # destructive — gated on a recoverable restore point
    if have_restore:
        if _run("repair_frontmatter.py") != 0:
            failed.append("repair-frontmatter")
        if _merge_ai_apply(recent) != 0:
            failed.append("merge")
        # repair broken episode links: rewrite slug-drift, prune unwatched/absent,
        # trigger ingest of listened-pending. Destructive (edits pages + ingests).
        if _run("repair_episode_links.py", "--write-log") != 0:
            failed.append("repair-links")
    else:
        print("[curate] no restore point — repair + merge SKIPPED", file=sys.stderr)

    # health check + deterministic fix, last: stubs broken topic/people/show
    # links the night's work may have left (verifies + repairs the link graph)
    if _run("lint.py", "--fix", "--write-log") != 0:
        failed.append("lint")

    # commit the run's net changes (best-effort; non-gating)
    try:
        git_checkpoint("post-curate")
    except Exception as e:
        print(f"[curate] post-commit failed: {e}", file=sys.stderr)

    if failed:
        print(f"[curate] FAILED: {' '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="podmind curate", description="wiki health & curation"
    )
    sub = ap.add_subparsers(dest="cmd", metavar="<subcommand>")
    for name in _FORWARDS:
        sub.add_parser(name, add_help=False)
    sub.add_parser("merge", add_help=False)
    p_night = sub.add_parser("nightly")
    p_night.add_argument("--recent", type=int, default=100)

    args, rest = ap.parse_known_args(argv)
    if not args.cmd:
        ap.print_help()
        return 1
    if args.cmd in _FORWARDS:
        return _run(_FORWARDS[args.cmd], *rest)
    if args.cmd == "merge":
        return _cmd_merge(rest)
    if args.cmd == "nightly":
        return nightly(args.recent)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
