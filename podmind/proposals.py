"""Parser for the merge-proposals checklist file.

`bin/merge_topic_dups.py --propose` writes a markdown file with one section
per candidate group. The user reviews in Obsidian, ticks groups they want
merged, optionally unticks individual members to exclude them, and then runs
`--apply-from`. This module is the canonical reader.

Format example:

    ## 1. `claude-code`

    - [x] merge group
    - canonical: `claude-code`
    - members:
      - [x] `claude-code` (71 citations) ← canonical
      - [x] `claude-code-skills` (12 citations)
      - [ ] `cursor-vs-claude-code` (1 citations)

Semantics (post-2026-04-29):
- `[x] merge group` = approve. `[ ]` = skip whole group.
- `[x] <slug>` = include in merge. `[ ]` = exclude. (Inverted earlier; reverted
  back to natural reading + ship a CSS snippet to suppress Obsidian's
  strikethrough on ticked tasks.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Proposal:
    """One merge proposal: which slugs collapse into which canonical."""
    canonical: str
    members: frozenset[str]


# Allow the very first section to start at position 0 (no leading newline) —
# real proposal files always have a preamble, but tests + small fixtures don't.
_SECTION_SPLIT = re.compile(r"(?:\A|\n)## \d+\. ")
_MERGE_GROUP_TICK = re.compile(r"^- \[x\] merge group\s*$", re.M | re.I)
_CANONICAL = re.compile(r"^- canonical: `([^`]+)`\s*$", re.M)
_MEMBER_TICKED = re.compile(r"^  - \[x\] `([^`]+)`", re.M | re.I)


def parse(text: str) -> list[Proposal]:
    """Parse the whole proposals file. Returns one `Proposal` per ticked group.

    A group is included iff:
      - The group-level `- [x] merge group` is ticked
      - At least 2 members are ticked
      - The canonical is among the ticked members
    """
    out: list[Proposal] = []
    sections = _SECTION_SPLIT.split(text)[1:]  # skip preamble
    for sec in sections:
        if not _MERGE_GROUP_TICK.search(sec):
            continue
        cm = _CANONICAL.search(sec)
        if not cm:
            continue
        canonical = cm.group(1)
        members = {m.group(1) for m in _MEMBER_TICKED.finditer(sec)}
        if len(members) < 2 or canonical not in members:
            continue
        out.append(Proposal(canonical=canonical, members=frozenset(members)))
    return out


def parse_file(path: Path) -> list[Proposal]:
    return parse(path.read_text(errors="ignore"))
