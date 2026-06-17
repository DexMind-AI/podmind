#!/usr/bin/env -S uv run python
"""repair_episode_links — repair broken episodes/ cross-links by cause.

Resolution is canonical and date-tolerant: each broken citation is matched to a
raw episode by show + title-token overlap (NOT exact date — page filenames use
pub_date while raw dirs use watched_at, so the dates drift). Whether that raw
episode already has a page is read from the `raw_dir` frontmatter map, not a
filename glob.

Buckets: slug_drift (page exists under a drifted slug) → rewrite; pending (raw,
listened, transcribed, no page) → trigger ingest; blocked (listened, no
transcript yet) → report; unwatched (raw, not consumed) / absent (no raw) →
prune; ambiguous (multiple raw candidates) → report.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(_BIN))
from _lib import WIKI_DIR  # noqa: E402
from podmind.frontmatter import read_raw_dir  # noqa: E402
import lint  # noqa: E402

DATA_ROOT = WIKI_DIR.parent
EP_DIR = WIKI_DIR / "episodes"
RAW = DATA_ROOT / "raw" / "episodes"
SHOW_DATE = re.compile(r"(.+?)-(\d{4}-\d{2}-\d{2})")
DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}-(.*)")
INGEST_CAP = 25
TITLE_MATCH = 0.5   # min title-token Jaccard to bind a citation to a raw episode
TITLE_GAP = 0.1     # min lead of best over 2nd-best (else ambiguous)
_BULLET = re.compile(r"^\s*[-*]\s")


def _tokens(s: str) -> set[str]:
    return {t for t in s.split("-") if t}


def _build_ingested_map() -> dict[str, str]:
    """raw_dir -> page_slug, from wiki/episodes `raw_dir` frontmatter (canonical)."""
    m: dict[str, str] = {}
    for p in EP_DIR.glob("*.md"):
        rd = read_raw_dir(p)
        if rd:
            m[rd] = p.stem
    return m


def resolve_raw(show: str, title: str) -> tuple[str, str | None]:
    """Match a citation to a raw episode dir under RAW/<show> by title overlap.

    Returns ("matched", dir_name) | ("ambiguous", None) | ("none", None).
    Date-tolerant: the raw dir's date need not equal the citation's date.
    """
    showdir = RAW / show
    if not showdir.is_dir():
        return ("none", None)
    want = _tokens(title)
    scored = []
    for d in sorted(showdir.iterdir()):
        if not d.is_dir():
            continue
        mm = DATE_PREFIX.match(d.name)
        rt = _tokens(mm.group(1) if mm else d.name)
        j = len(want & rt) / len(want | rt) if (want | rt) else 0.0
        scored.append((j, d.name))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < TITLE_MATCH:
        return ("none", None)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < TITLE_GAP:
        return ("ambiguous", None)
    return ("matched", scored[0][1])


def _listened_meta(raw_dir: str) -> tuple[bool, bool]:
    """(listened, has_transcript) for a raw_dir."""
    try:
        d = json.loads((RAW / raw_dir / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return (False, False)
    dur = d.get("duration_sec") or 0
    played = d.get("played_up_to") or 0
    listened = bool(d.get("listened")) or (dur > 0 and played / dur >= 0.5)
    has_tx = d.get("transcript_source") not in (None, "none")
    return (listened, has_tx)


def categorize(broken: list[tuple[str, str]]) -> dict[str, list[dict]]:
    by_target: dict[str, list[str]] = defaultdict(list)
    for src, target in broken:
        if target.startswith("episodes/"):
            by_target[target].append(src)
    ingested = _build_ingested_map()
    buckets: dict[str, list[dict]] = {
        "slug_drift": [], "pending": [], "blocked": [],
        "unwatched": [], "absent": [], "ambiguous": [],
    }
    for target in sorted(by_target):
        srcs = sorted(set(by_target[target]))
        stem = target.split("/", 1)[1]
        if (EP_DIR / f"{stem}.md").exists():
            continue  # exact page exists — not broken
        m = SHOW_DATE.match(stem)
        if not m:
            buckets["absent"].append({"target": target, "sources": srcs}); continue
        show = m.group(1)
        title = stem[m.end():].lstrip("-")
        status, raw_name = resolve_raw(show, title)
        if status == "ambiguous":
            buckets["ambiguous"].append({"target": target, "sources": srcs}); continue
        if status == "none":
            buckets["absent"].append({"target": target, "sources": srcs}); continue
        raw_dir = f"{show}/{raw_name}"
        if raw_dir in ingested:
            buckets["slug_drift"].append(
                {"target": target, "sources": srcs, "page_slug": ingested[raw_dir]}); continue
        listened, has_tx = _listened_meta(raw_dir)
        entry = {"target": target, "sources": srcs, "raw_dir": raw_dir}
        if not listened:
            buckets["unwatched"].append(entry)
        elif has_tx:
            buckets["pending"].append(entry)
        else:
            buckets["blocked"].append(entry)  # listened, awaiting transcript
    return buckets


def _src_path(src: str) -> Path:
    kind, stem = src.split("/", 1)
    return WIKI_DIR / kind / f"{stem}.md"


def rewrite_in_sources(entry: dict) -> str:
    """Rewrite the broken link to the known canonical page slug in every source."""
    broken = entry["target"].split("/", 1)[1]
    match = entry["page_slug"]
    pat = re.compile(r"\[\[episodes/" + re.escape(broken) + r"((?:\|[^\]]+)?)\]\]")
    for src in entry["sources"]:
        p = _src_path(src)
        if not p.exists():
            continue
        p.write_text(pat.sub(lambda m: f"[[episodes/{match}{m.group(1)}]]", p.read_text()))
    return match


def prune_in_sources(entry: dict) -> int:
    """Remove citation bullet lines containing the dead link. Inline prose left."""
    target = entry["target"].split("/", 1)[1]
    pat = re.compile(r"\[\[episodes/" + re.escape(target) + r"(?:\|[^\]]+)?\]\]")
    pruned = 0
    for src in entry["sources"]:
        p = _src_path(src)
        if not p.exists():
            continue
        out = []
        for line in p.read_text().split("\n"):
            if pat.search(line) and _BULLET.match(line):
                pruned += 1
                continue
            out.append(line)
        p.write_text("\n".join(out))
    return pruned


def trigger_ingest(raw_dirs: list[str], cap: int = INGEST_CAP) -> tuple[int, int]:
    """Ingest up to `cap` raw episodes; return (pages_actually_written, overflow)."""
    targeted = raw_dirs[:cap]
    overflow = max(0, len(raw_dirs) - cap)
    if not targeted:
        return 0, overflow
    proc = subprocess.run(
        [sys.executable, str(_BIN / "ingest_run.py"), "--only", ",".join(targeted)],
        capture_output=True, text=True,
    )
    m = re.search(r"Wrote (\d+) episode pages", proc.stdout)
    return (int(m.group(1)) if m else 0), overflow


def repair() -> dict:
    broken = [(s, t) for s, t in lint.find_broken_links() if t.startswith("episodes/")]
    b = categorize(broken)
    rewrote = sum(1 for e in b["slug_drift"] if rewrite_in_sources(e))
    pruned = sum(prune_in_sources(e) for e in b["unwatched"] + b["absent"])
    ingested, overflow = trigger_ingest([e["raw_dir"] for e in b["pending"]])
    return {
        "rewrote": rewrote,
        "pruned": pruned,
        "ingested": ingested,
        "still_pending": overflow + len(b["blocked"]),
        "ambiguous": len(b["ambiguous"]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repair_episode_links")
    ap.add_argument("--write-log", action="store_true")
    args = ap.parse_args(argv)
    r = repair()
    line = (f"episode-link repair: rewrote {r['rewrote']}, pruned {r['pruned']}, "
            f"ingested {r['ingested']}, still-pending {r['still_pending']}, "
            f"ambiguous {r['ambiguous']}")
    print(line)
    if args.write_log:
        log = WIKI_DIR / "log.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        log.write_text(log.read_text() + f"\n## [{ts}] repair-links\n- {line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
