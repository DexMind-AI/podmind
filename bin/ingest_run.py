#!/usr/bin/env -S uv run python
"""ingest_run — full backlog driver: prep → LLM summarize → finalize, in one command.

Usage: ./bin/ingest_run.py [N] [--batch-name <int>] [--concurrency 8]

Replaces the Claude Code agent dispatch loop. Run from a normal shell — no LLM
parent needed.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# CODE_ROOT = podmind code repo (where bin/ scripts live)
# DATA_ROOT = vault (where wiki/log.md, wiki/episodes/ live)
CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))
from podmind import paths as _paths  # noqa: E402
DATA_ROOT = _paths.DATA_ROOT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=12)
    ap.add_argument("--batch-name", type=int, help="batch number for log entry; default=auto from log")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tick_file = "/tmp/tick.json"
    print(f"[1/3] tick_prep — finding next {args.n} pending...")
    with open(tick_file, "w") as f:
        subprocess.run([sys.executable, str(CODE_ROOT / "bin/tick_prep.py"), str(args.n)], stdout=f, check=True)
    d = json.loads(Path(tick_file).read_text())
    print(f"      pending {d['pending_total']}, auto-quarantined {len(d['quarantined_this_run'])}, dispatching {len(d['dispatch'])}")
    for e in d["dispatch"]:
        print(f"      {e['idx']}  {e['size']:>7}  {e['raw_dir']}")

    if args.dry_run:
        return

    # Clear any prior results
    for f in Path("/tmp/podmind-results").glob("*.json"):
        f.unlink()

    print(f"\n[2/3] LLM summarize — calling the configured provider (concurrency={args.concurrency})...")
    subprocess.run(
        [str(CODE_ROOT / "bin/summarize.py"), tick_file, "--concurrency", str(args.concurrency)],
        check=True,
    )

    if not args.batch_name:
        # Auto-pick: last `batch N` in log + 1
        log = (DATA_ROOT / "wiki/log.md").read_text()
        import re
        nums = [int(m) for m in re.findall(r"batch[:\s]+(\d+)", log)]
        args.batch_name = max(nums) + 1 if nums else 1

    print(f"\n[3/3] tick_finalize — batch {args.batch_name}...")
    note = f"LLM backlog drain ({len(d['dispatch'])} dispatched)"
    cmd = [str(CODE_ROOT / "bin/tick_finalize.py"), str(args.batch_name), "--note", note]
    for q in d["quarantined_this_run"]:
        cmd += ["--quarantined-dups", f"{q['rd']} = {q['dup_of']}"]
    subprocess.run(cmd, check=True)

    print(f"\n✓ done. {datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    main()
