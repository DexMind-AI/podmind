#!/usr/bin/env -S uv run python
"""sensitivity_audit — flag wiki episode pages whose source transcript hits China-PRC
sensitive markers. Use after a backlog ingest run to identify candidates for
re-run via Sonnet/Opus (the hosted DeepSeek API softens or omits sensitive framings).

Usage: ./bin/sensitivity_audit.py [--threshold 3] [--out flagged.txt]
"""
import argparse
import re
from pathlib import Path

from _lib import EPISODES_DIR, WIKI_DIR, parse_raw_dir

# Patterns are designed for high recall; threshold filters to genuine topic hits
SENSITIVE_PATTERNS: dict[str, str] = {
    "tiananmen": r"\b(tiananmen|june\s*4(?:th)?\s*1989|tank\s*man)\b",
    "xinjiang": r"\b(xinjiang|uyghur|uighur|re-education\s*camp|vocational\s*training\s*center)\b",
    "hong_kong_protest": r"\b(umbrella\s*movement|hong\s*kong\s*protest|national\s*security\s*law|joshua\s*wong)\b",
    "taiwan": r"\b(taiwan\s*independence|taiwan\s*strait|cross-strait|tsmc\s*invasion|one\s*china\s*policy)\b",
    "xi_critique": r"\b(xi\s*jinping(?:'s)?\s*(?:wealth|family|succession|cult)|paramount\s*leader|emperor\s*xi)\b",
    "ccp_critique": r"\b(ccp\s*legitimacy|chinese\s*communist\s*party|one-party\s*state|authoritarian\s*china)\b",
    "dissidents": r"\b(falun\s*gong|liu\s*xiaobo|ai\s*weiwei|jimmy\s*lai|peng\s*shuai)\b",
    "south_china_sea": r"\b(south\s*china\s*sea|nine-?dash\s*line|spratly|paracel)\b",
    "tibet": r"\b(tibet|dalai\s*lama|tibetan\s*independence)\b",
}


def scan_transcript(text: str) -> dict[str, int]:
    text_lower = text.lower()
    return {
        cat: len(re.findall(pat, text_lower, re.IGNORECASE))
        for cat, pat in SENSITIVE_PATTERNS.items()
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=3, help="min combined hits to flag")
    ap.add_argument("--out", default="-", help="output file or - for stdout")
    args = ap.parse_args()

    flagged: list[tuple[Path, str, int, dict[str, int]]] = []
    for ep in (WIKI_DIR / "episodes").glob("*.md"):
        rd = parse_raw_dir(ep)
        if not rd:
            continue
        tpath = EPISODES_DIR / rd / "transcript.md"
        if not tpath.exists():
            continue
        # Skip the .corrupt files
        try:
            text = tpath.read_text(errors="ignore")
        except OSError:
            continue
        hits = scan_transcript(text)
        total = sum(hits.values())
        if total >= args.threshold:
            flagged.append((ep, rd, total, hits))

    flagged.sort(key=lambda x: -x[2])

    out_lines = []
    out_lines.append(f"# Sensitivity audit — {len(flagged)} episodes flagged at threshold≥{args.threshold}\n")
    for ep, rd, total, hits in flagged:
        active = ", ".join(f"{c}:{n}" for c, n in hits.items() if n > 0)
        out_lines.append(f"{total:>3}  {ep.stem}")
        out_lines.append(f"     raw: {rd}")
        out_lines.append(f"     hits: {active}")
        out_lines.append("")

    output = "\n".join(out_lines)
    if args.out == "-":
        print(output)
    else:
        Path(args.out).write_text(output)
        print(f"wrote {args.out} ({len(flagged)} flagged)")


if __name__ == "__main__":
    main()
