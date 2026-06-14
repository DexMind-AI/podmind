"""Tests for podmind.frontmatter.

This is the canonical YAML-frontmatter parser for wiki pages. Five different
ad-hoc parsers used to do this job; consolidating them surfaced edge cases
that the line-by-line scans silently mishandled.
"""
import textwrap

import pytest

from podmind.frontmatter import (
    EpisodePage,
    first_body_line,
    parse,
    read_raw_dir,
    read_date,
)


def _dedent(s: str) -> str:
    """Strip the leading newline + indent from a triple-quoted block."""
    return textwrap.dedent(s).lstrip("\n")


# ---- parse() core behavior ---------------------------------------------------


class TestParse:
    def test_standard_episode(self):
        text = _dedent("""
            ---
            show: The Breakdown
            date: 2026-04-29
            listened: true
            played_up_to: 1200
            duration_min: 45
            guests: [Andy Stumpf]
            transcript_source: rss
            raw_dir: the-breakdown/2026-04-29-iran-strike
            ---

            🎧 [[shows/the-breakdown]] — Iran strike analysis with Andy Stumpf.

            ## Key takeaways
        """)
        fm, body = parse(text)
        assert fm["show"] == "The Breakdown"
        assert str(fm["date"]) == "2026-04-29"
        assert fm["listened"] is True
        assert fm["played_up_to"] == 1200
        assert fm["guests"] == ["Andy Stumpf"]
        assert fm["raw_dir"] == "the-breakdown/2026-04-29-iran-strike"
        assert body.startswith("🎧 [[shows/the-breakdown]]")

    def test_no_frontmatter(self):
        """Plain markdown without frontmatter returns ({}, original_text)."""
        text = "# Just a title\n\nBody text."
        fm, body = parse(text)
        assert fm == {}
        assert body == text

    def test_empty_frontmatter(self):
        text = "---\n---\n\nBody."
        fm, body = parse(text)
        assert fm == {}
        assert body == "Body."

    def test_quoted_string_values(self):
        text = _dedent("""
            ---
            show: "The 'Quoted' Show"
            raw_dir: "show-with-quotes/2026-01-01-episode"
            ---

            body
        """)
        fm, _ = parse(text)
        assert fm["show"] == "The 'Quoted' Show"
        assert fm["raw_dir"] == "show-with-quotes/2026-01-01-episode"

    def test_list_values(self):
        text = _dedent("""
            ---
            guests: [Alice, Bob, Charlie]
            ---

            body
        """)
        fm, _ = parse(text)
        assert fm["guests"] == ["Alice", "Bob", "Charlie"]

    def test_empty_guests_list(self):
        text = "---\nguests: []\n---\n\nbody"
        fm, _ = parse(text)
        assert fm["guests"] == []

    def test_malformed_yaml_returns_empty_fm(self):
        """Don't crash on bad YAML — degrade gracefully so the body stays accessible."""
        text = "---\n: : : malformed : :\n---\n\nbody"
        fm, body = parse(text)
        assert fm == {}
        assert body == "body"

    def test_frontmatter_without_trailing_newline_after_closing(self):
        """`---<eof>` (no body) shouldn't crash."""
        text = "---\nshow: Foo\n---"
        fm, body = parse(text)
        assert fm["show"] == "Foo"
        assert body == ""

    def test_frontmatter_with_no_blank_line_before_body(self):
        """Some old pages don't have a blank line between `---` and body."""
        text = "---\nshow: Foo\n---\nBody starts immediately."
        fm, body = parse(text)
        assert fm["show"] == "Foo"
        assert body == "Body starts immediately."

    def test_frontmatter_must_start_at_position_0(self):
        """A `---` later in the file is not frontmatter."""
        text = "Some text\n---\nshow: Not Frontmatter\n---\n"
        fm, body = parse(text)
        assert fm == {}
        assert body == text

    def test_yaml_returning_non_dict_treated_as_no_fm(self):
        """`yaml.safe_load` of `--- 42\n` returns int 42, not a dict — handle gracefully."""
        text = "---\n42\n---\n\nbody"
        fm, body = parse(text)
        assert fm == {}


# ---- first_body_line() ------------------------------------------------------


class TestFirstBodyLine:
    def test_skips_blank_lines(self):
        assert first_body_line("\n\n  \n🎧 hook line\nrest\n") == "🎧 hook line"

    def test_returns_empty_for_empty_body(self):
        assert first_body_line("") == ""
        assert first_body_line("\n\n\n") == ""

    def test_single_line(self):
        assert first_body_line("just one line") == "just one line"


# ---- EpisodePage typed view -------------------------------------------------


class TestEpisodePage:
    @pytest.fixture
    def standard_page(self):
        return _dedent("""
            ---
            show: The Breakdown
            date: 2026-04-29
            listened: true
            played_up_to: 1200
            duration_min: 45
            guests: [Andy Stumpf]
            transcript_source: rss
            raw_dir: the-breakdown/2026-04-29-iran-strike
            ---

            🎧 [[shows/the-breakdown]] — Iran strike analysis with Andy Stumpf.

            ## Key takeaways
        """)

    def test_from_text_populates_all_fields(self, standard_page):
        ep = EpisodePage.from_text(standard_page)
        assert ep.raw_dir == "the-breakdown/2026-04-29-iran-strike"
        assert ep.date == "2026-04-29"
        assert ep.show == "The Breakdown"
        assert ep.listened is True
        assert ep.played_up_to == 1200
        assert ep.duration_min == 45
        assert ep.guests == ["Andy Stumpf"]
        assert ep.transcript_source == "rss"
        assert ep.hook.startswith("🎧 [[shows/the-breakdown]]")
        assert ep.body.startswith("🎧 [[shows/the-breakdown]]")

    def test_missing_fields_get_safe_defaults(self):
        """A minimal page should still produce a usable EpisodePage."""
        text = "---\nshow: Foo\n---\n\nbody"
        ep = EpisodePage.from_text(text)
        assert ep.show == "Foo"
        assert ep.raw_dir is None
        assert ep.date is None
        assert ep.listened is False
        assert ep.played_up_to == 0
        assert ep.duration_min == 0
        assert ep.guests == []
        assert ep.transcript_source is None

    def test_played_up_to_with_null_value(self):
        """`played_up_to: null` (or omitted) → 0, not crash."""
        text = "---\nplayed_up_to: null\n---\n\nbody"
        ep = EpisodePage.from_text(text)
        assert ep.played_up_to == 0

    def test_no_frontmatter_at_all(self):
        text = "# Just a heading\n\nNo frontmatter here."
        ep = EpisodePage.from_text(text)
        assert ep.raw_dir is None
        assert ep.show is None
        assert ep.body == text


# ---- File-level convenience -------------------------------------------------


class TestFileHelpers:
    def test_read_raw_dir(self, tmp_path):
        p = tmp_path / "ep.md"
        p.write_text("---\nraw_dir: yt-foo/2026-01-01-bar\n---\n\nbody")
        assert read_raw_dir(p) == "yt-foo/2026-01-01-bar"

    def test_read_raw_dir_missing(self, tmp_path):
        p = tmp_path / "ep.md"
        p.write_text("---\nshow: Foo\n---\n\nbody")
        assert read_raw_dir(p) is None

    def test_read_raw_dir_no_frontmatter(self, tmp_path):
        p = tmp_path / "ep.md"
        p.write_text("# Plain markdown")
        assert read_raw_dir(p) is None

    def test_read_date_normalizes_to_string(self, tmp_path):
        """YAML auto-parses date: 2026-04-29 to a date object; we want str."""
        p = tmp_path / "ep.md"
        p.write_text("---\ndate: 2026-04-29\n---\n\nbody")
        assert read_date(p) == "2026-04-29"


class TestColonInShowName:
    """Regression: a colon in an unquoted scalar breaks the WHOLE frontmatter
    block, silently zeroing raw_dir. This corrupted 992 pages (2026-06-11),
    which were then re-ingested daily. tick_finalize now quotes scalars and
    write-then-verifies; these tests lock the underlying YAML invariant."""

    def test_unquoted_colon_breaks_frontmatter(self, tmp_path):
        """Documents the bug: unquoted 'Real Vision: Finance' → parse fails →
        raw_dir invisible."""
        p = tmp_path / "ep.md"
        p.write_text(
            "---\n"
            "show: Real Vision: Finance & Investing\n"
            "raw_dir: real-vision/2026-06-08-panic\n"
            "---\n\nbody"
        )
        assert read_raw_dir(p) is None  # the failure mode we hit

    def test_quoted_colon_round_trips(self, tmp_path):
        """The fix: double-quoting the show name makes the block valid, so
        raw_dir reads back correctly."""
        p = tmp_path / "ep.md"
        p.write_text(
            "---\n"
            'show: "Real Vision: Finance & Investing"\n'
            'raw_dir: "real-vision/2026-06-08-panic"\n'
            "---\n\nbody"
        )
        fm, _ = parse(p.read_text())
        assert fm["show"] == "Real Vision: Finance & Investing"
        assert read_raw_dir(p) == "real-vision/2026-06-08-panic"
