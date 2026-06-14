"""Tests for podmind.proposals.parse.

The proposals checklist is the user's interface for approving merges. Subtle
parsing bugs here mean either silent skips (missed merges) or accidental
applies (wrong merges). Pin every documented case.
"""
import textwrap

import pytest

from podmind.proposals import Proposal, parse


def _dedent(s: str) -> str:
    return textwrap.dedent(s).lstrip("\n")


class TestApprovedGroups:
    def test_single_group_all_members_ticked(self):
        text = _dedent("""
            # header

            ## 1. `ai-agents`

            - [x] merge group
            - canonical: `ai-agents`
            - members:
              - [x] `ai-agents` (10 citations) ← canonical
              - [x] `agents-ai` (3 citations)
        """)
        result = parse(text)
        assert len(result) == 1
        assert result[0] == Proposal(canonical="ai-agents", members=frozenset({"ai-agents", "agents-ai"}))

    def test_member_unticked_is_excluded(self):
        text = _dedent("""
            ## 1. `claude-code`

            - [x] merge group
            - canonical: `claude-code`
            - members:
              - [x] `claude-code` (71 citations) ← canonical
              - [x] `claude-code-skills` (12 citations)
              - [ ] `cursor-vs-claude-code` (1 citations)
        """)
        result = parse(text)
        assert len(result) == 1
        assert "cursor-vs-claude-code" not in result[0].members
        assert result[0].members == frozenset({"claude-code", "claude-code-skills"})

    def test_multiple_groups_only_ticked_returned(self):
        text = _dedent("""
            ## 1. `merged`

            - [x] merge group
            - canonical: `merged`
            - members:
              - [x] `merged`
              - [x] `also-merged`

            ## 2. `skipped`

            - [ ] merge group
            - canonical: `skipped`
            - members:
              - [x] `skipped`
              - [x] `skipped-also`

            ## 3. `another-merged`

            - [x] merge group
            - canonical: `another-merged`
            - members:
              - [x] `another-merged`
              - [x] `friend`
        """)
        result = parse(text)
        canonicals = {p.canonical for p in result}
        assert canonicals == {"merged", "another-merged"}


class TestSkippedGroups:
    def test_unticked_group(self):
        """Group-level `[ ] merge group` → skip entirely."""
        text = _dedent("""
            ## 1. `not-approved`

            - [ ] merge group
            - canonical: `not-approved`
            - members:
              - [x] `not-approved`
              - [x] `friend`
        """)
        assert parse(text) == []

    def test_only_one_member_ticked(self):
        """Need ≥2 ticked members to merge anything."""
        text = _dedent("""
            ## 1. `lonely`

            - [x] merge group
            - canonical: `lonely`
            - members:
              - [x] `lonely`
              - [ ] `would-have-been-merged`
        """)
        assert parse(text) == []

    def test_canonical_unticked(self):
        """If the canonical itself is unticked, skip — can't merge into nothing."""
        text = _dedent("""
            ## 1. `original-canonical`

            - [x] merge group
            - canonical: `original-canonical`
            - members:
              - [ ] `original-canonical`
              - [x] `member-a`
              - [x] `member-b`
        """)
        assert parse(text) == []

    def test_missing_canonical_field(self):
        text = _dedent("""
            ## 1. `something`

            - [x] merge group
            - members:
              - [x] `a`
              - [x] `b`
        """)
        assert parse(text) == []


class TestMalformedInput:
    def test_empty_file(self):
        assert parse("") == []

    def test_no_sections(self):
        assert parse("# Just a header, no proposals.") == []

    def test_section_without_required_fields(self):
        text = "## 1. `bare`\n\nJust some prose, no checkboxes."
        assert parse(text) == []

    def test_uppercase_X_in_checkbox(self):
        """Obsidian sometimes saves `[X]` instead of `[x]`."""
        text = _dedent("""
            ## 1. `case-test`

            - [X] merge group
            - canonical: `case-test`
            - members:
              - [X] `case-test`
              - [X] `friend`
        """)
        result = parse(text)
        assert len(result) == 1


class TestRoundtripWithExclusions:
    """The end-to-end semantic: ticks define inclusion, unticks define exclusion."""

    def test_only_canonical_and_one_other_ticked(self):
        text = _dedent("""
            ## 1. `iran-nuclear`

            - [x] merge group
            - canonical: `iran-nuclear`
            - members:
              - [x] `iran-nuclear` ← canonical
              - [x] `iran-nuclear-talks`
              - [ ] `iran-war`
              - [ ] `israel-iran-war`
              - [ ] `iran-proxy-war`
        """)
        result = parse(text)
        assert len(result) == 1
        assert result[0].members == frozenset({"iran-nuclear", "iran-nuclear-talks"})
