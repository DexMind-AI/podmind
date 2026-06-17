"""Tests for bin/lint.py auto-fix (broken-link stubs)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import lint  # noqa: E402


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    for kind in ("episodes", "people", "topics", "shows"):
        (tmp_path / kind).mkdir()
    monkeypatch.setattr(lint, "WIKI_DIR", tmp_path)
    return tmp_path


class TestMakeStub:
    def test_creates_topic_stub_with_title_marker_and_backlinks(self, wiki):
        created = lint.make_stub("topics/foo-bar", ["episodes/x", "topics/y"])
        assert created is True
        text = (wiki / "topics" / "foo-bar.md").read_text()
        assert text.startswith("# Foo Bar\n")
        assert "lint --fix" in text          # Phase 2 handoff marker
        assert "## Citations" in text
        assert "- [[episodes/x]]" in text
        assert "- [[topics/y]]" in text

    def test_shows_use_episodes_header(self, wiki):
        lint.make_stub("shows/some-show", ["episodes/x"])
        text = (wiki / "shows" / "some-show.md").read_text()
        assert "## Episodes" in text
        assert "## Citations" not in text

    def test_refuses_to_overwrite_existing(self, wiki):
        existing = wiki / "topics" / "foo.md"
        existing.write_text("# Real Page\nhand-authored\n")
        created = lint.make_stub("topics/foo", ["episodes/x"])
        assert created is False
        assert existing.read_text() == "# Real Page\nhand-authored\n"


class TestFixBrokenLinks:
    def test_stubs_safe_namespaces_reports_episodes(self, wiki):
        broken = [
            ("episodes/a", "topics/new-topic"),
            ("episodes/b", "topics/new-topic"),   # same target, 2 sources
            ("topics/c", "people/new-person"),
            ("episodes/d", "shows/new-show"),
            ("topics/e", "episodes/missing-ep"),  # episode → report only
        ]
        created, reported = lint.fix_broken_links(broken)

        assert set(created) == {"topics/new-topic", "people/new-person", "shows/new-show"}
        assert reported == ["episodes/missing-ep"]
        assert not (wiki / "episodes" / "missing-ep.md").exists()  # never fabricated
        # grouped backlinks: both sources land in the one stub
        topic_text = (wiki / "topics" / "new-topic.md").read_text()
        assert "- [[episodes/a]]" in topic_text and "- [[episodes/b]]" in topic_text

    def test_idempotent_second_run_creates_nothing(self, wiki):
        broken = [("episodes/a", "topics/foo")]
        first, _ = lint.fix_broken_links(broken)
        assert first == ["topics/foo"]
        # second run: the file now exists, so make_stub returns False
        second, _ = lint.fix_broken_links(broken)
        assert second == []


class TestTemplateAndJunkGuards:
    def test_find_broken_links_skips_underscore_template_sources(self, wiki):
        # _template holds placeholder links that aren't real references
        (wiki / "episodes" / "_template.md").write_text("[[topics/...]]\n[[shows/<show-slug>]]\n")
        # a real page with a genuine broken link
        (wiki / "topics" / "real.md").write_text("see [[topics/genuinely-missing]]\n")
        broken = lint.find_broken_links()
        targets = {t for _, t in broken}
        assert "topics/genuinely-missing" in targets
        assert "topics/..." not in targets
        assert "shows/<show-slug>" not in targets

    def test_fix_skips_invalid_slug_targets(self, wiki):
        broken = [
            ("episodes/x", "topics/..."),
            ("episodes/y", "shows/<show-slug>"),
            ("episodes/z", "topics/good-topic"),
        ]
        created, reported = lint.fix_broken_links(broken)
        assert created == ["topics/good-topic"]
        assert not (wiki / "topics" / "....md").exists()
        assert not (wiki / "shows" / "<show-slug>.md").exists()
