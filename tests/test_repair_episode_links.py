"""Tests for bin/repair_episode_links.py (frontmatter-based, date-tolerant)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import repair_episode_links as rel  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    for k in ("episodes", "people", "topics", "shows"):
        (tmp_path / "wiki" / k).mkdir(parents=True)
    (tmp_path / "raw" / "episodes").mkdir(parents=True)
    monkeypatch.setattr(rel, "WIKI_DIR", tmp_path / "wiki")
    monkeypatch.setattr(rel, "EP_DIR", tmp_path / "wiki" / "episodes")
    monkeypatch.setattr(rel, "RAW", tmp_path / "raw" / "episodes")
    return tmp_path


def _raw(vault, show, name, *, played=100, dur=100, tx="youtube"):
    d = vault / "raw" / "episodes" / show / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"duration_sec": dur, "played_up_to": played,
         "listened": played >= dur, "transcript_source": tx}))


def _page(vault, slug, raw_dir):
    (vault / "wiki" / "episodes" / f"{slug}.md").write_text(
        f"---\nraw_dir: {raw_dir}\n---\n# x\n")


class TestResolveRaw:
    def test_date_tolerant_truncated_title(self, vault):
        _raw(vault, "s", "2026-06-26-build-muscle-recovery")
        assert rel.resolve_raw("s", "build-muscle-recov")[0] == "matched"

    def test_no_match(self, vault):
        _raw(vault, "s", "2026-01-01-something-else")
        assert rel.resolve_raw("s", "totally-different-topic") == ("none", None)

    def test_ambiguous_tie(self, vault):
        _raw(vault, "s", "2026-01-01-ai-news-update")
        _raw(vault, "s", "2026-01-02-ai-news-update")
        assert rel.resolve_raw("s", "ai-news-update")[0] == "ambiguous"


class TestCategorize:
    def test_slug_drift_when_page_date_differs_from_raw_date(self, vault):
        # THE BUG: raw dir uses watched date; page filename uses a different
        # (pub) date. A filename glob on the citation's date misses the page;
        # the raw_dir frontmatter map finds it.
        _raw(vault, "show", "2026-06-26-gemini-cli-tutorial")
        _page(vault, "show-2026-06-20-gemini-cli-tutorial", "show/2026-06-26-gemini-cli-tutorial")
        b = rel.categorize([("people/p", "episodes/show-2026-06-26-gemini-cli-tutorial")])
        assert b["slug_drift"] == [{
            "target": "episodes/show-2026-06-26-gemini-cli-tutorial",
            "sources": ["people/p"],
            "page_slug": "show-2026-06-20-gemini-cli-tutorial",
        }]
        assert b["pending"] == [] and b["absent"] == []

    def test_pending_blocked_unwatched_absent(self, vault):
        _raw(vault, "s", "2026-01-01-listened-tx", played=100, tx="youtube")
        _raw(vault, "s", "2026-02-02-listened-notx", played=100, tx="none")
        _raw(vault, "s", "2026-03-03-unwatched", played=0, tx="youtube")
        broken = [
            ("t/t", "episodes/s-2026-01-01-listened-tx"),
            ("t/t", "episodes/s-2026-02-02-listened-notx"),
            ("t/t", "episodes/s-2026-03-03-unwatched"),
            ("t/t", "episodes/s-2099-09-09-no-raw-here"),
        ]
        b = rel.categorize(broken)
        assert b["pending"][0]["raw_dir"] == "s/2026-01-01-listened-tx"
        assert b["blocked"][0]["raw_dir"] == "s/2026-02-02-listened-notx"
        assert b["unwatched"][0]["raw_dir"] == "s/2026-03-03-unwatched"
        assert [e["target"] for e in b["absent"]] == ["episodes/s-2099-09-09-no-raw-here"]


class TestRewrite:
    def test_rewrite_uses_page_slug_and_preserves_alias(self, vault):
        src = vault / "wiki/people/p.md"
        src.write_text("- [[episodes/show-2026-06-26-broken|alias]] note\n")
        rel.rewrite_in_sources({"target": "episodes/show-2026-06-26-broken",
                                "sources": ["people/p"],
                                "page_slug": "show-2026-06-20-broken-fixed"})
        assert "[[episodes/show-2026-06-20-broken-fixed|alias]]" in src.read_text()


class TestPrune:
    def test_prune_removes_only_target_bullet(self, vault):
        src = vault / "wiki/topics/t.md"
        src.write_text("## Citations\n- [[episodes/keep]] a\n- [[episodes/s-2026-01-01-dead]] b\n- [[episodes/also]] c\n")
        n = rel.prune_in_sources({"target": "episodes/s-2026-01-01-dead", "sources": ["topics/t"]})
        text = src.read_text()
        assert "keep" in text and "also" in text and "dead" not in text and n == 1

    def test_prune_leaves_inline_prose(self, vault):
        src = vault / "wiki/topics/t.md"
        src.write_text("As in [[episodes/s-2026-01-01-dead]] earlier.\n")
        rel.prune_in_sources({"target": "episodes/s-2026-01-01-dead", "sources": ["topics/t"]})
        assert "dead" in src.read_text()


class TestIngest:
    def test_parses_written_count(self, monkeypatch):
        class _P:
            stdout = "[3/3] tick_finalize...\nWrote 3 episode pages.\n"
        monkeypatch.setattr(rel.subprocess, "run", lambda *a, **k: _P())
        n, overflow = rel.trigger_ingest(["a/b", "c/d"])
        assert n == 3 and overflow == 0

    def test_caps(self, monkeypatch):
        class _P:
            stdout = "Wrote 25 episode pages."
        monkeypatch.setattr(rel.subprocess, "run", lambda *a, **k: _P())
        dirs = [f"s/2026-01-{i:02d}-e" for i in range(1, 40)]
        n, overflow = rel.trigger_ingest(dirs)
        assert overflow == 39 - rel.INGEST_CAP
