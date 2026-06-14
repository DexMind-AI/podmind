"""Tests for podmind.enrich (embedding-based cross-link enrichment).

Mocks `embed_texts` and `load` so no API key is needed and the geometry is
deterministic. Validates: text-extraction, neighbor exclusion, threshold
gating (auto-add vs suggest-only), and the in-place page edit.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from podmind import enrich, embeddings


@pytest.fixture
def episode_page(tmp_path):
    """A realistic-ish episode page with frontmatter + cross-links section."""
    p = tmp_path / "ep.md"
    p.write_text("""---
show: Test Show
date: 2026-01-01
listened: true
played_up_to: 0
duration_min: 45
guests: [Test Guest]
transcript_source: rss
raw_dir: test-show/2026-01-01-test-episode
---

🎧 [[shows/test-show]] — A discussion of agentic AI orchestration patterns and Claude Code workflows.

## Key takeaways

- Agentic patterns are emerging
- Claude Code is the leading tool
- Workflows compose

## Cross-links

- People: [[people/existing-person]]
- Topics: [[topics/existing-topic]]
""")
    return p


def _mock_caches(monkeypatch, topic_slugs, people_slugs, dim=4):
    """Patch embeddings.load + embed_texts with controlled vectors."""
    # All vectors point in different directions; we'll control similarity
    # by making the query vector overlap with specific cached ones.
    topic_vecs = np.eye(len(topic_slugs), dim, dtype=np.float32) if len(topic_slugs) <= dim else \
        np.random.RandomState(0).rand(len(topic_slugs), dim).astype(np.float32)
    people_vecs = np.eye(len(people_slugs), dim, dtype=np.float32) if len(people_slugs) <= dim else \
        np.random.RandomState(1).rand(len(people_slugs), dim).astype(np.float32)

    topics_cache = embeddings.EmbedResult(
        ids=np.array(topic_slugs, dtype=object),
        vecs=topic_vecs,
        model="test-model",
        dim=dim,
    )
    people_cache = embeddings.EmbedResult(
        ids=np.array(people_slugs, dtype=object),
        vecs=people_vecs,
        model="test-model",
        dim=dim,
    )

    def fake_load(name):
        if name == "topics": return topics_cache
        if name == "people": return people_cache
        raise FileNotFoundError(name)

    def fake_embed(texts, **kwargs):
        # Query vector aligned to first topic + first person → highest cosine
        return np.array([[1.0, 0.5, 0.0, 0.0]], dtype=np.float32)

    monkeypatch.setattr(embeddings, "load", fake_load)
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)


class TestEpisodeQueryText:
    def test_extracts_hook_and_takeaways(self):
        body = """🎧 [[shows/x]] — The hook line.

## Key takeaways

- First takeaway
- Second takeaway

## Cross-links

- Topics: [[topics/foo]]
"""
        result = enrich.episode_query_text(body)
        assert "🎧 [[shows/x]] — The hook line." in result
        assert "First takeaway" in result
        assert "Topics: [[topics/foo]]" not in result  # cross-links excluded

    def test_empty_body(self):
        assert enrich.episode_query_text("") == ""
        assert enrich.episode_query_text("   \n\n  ") == ""

    def test_hook_only_no_takeaways(self):
        body = "Just a hook with no takeaways block.\n"
        assert "Just a hook" in enrich.episode_query_text(body)


class TestEnrichEpisode:
    def test_high_cosine_auto_added(self, episode_page, monkeypatch):
        # First topic gets perfect cosine (1.0), well above auto_threshold 0.55
        _mock_caches(monkeypatch, ["very-relevant-topic", "irrelevant"], ["very-relevant-person", "irrelevant-p"])
        report = enrich.enrich_episode(episode_page)
        assert "very-relevant-topic" in report["added_topics"]
        assert "very-relevant-person" in report["added_people"]
        # The page now contains the new links
        text = episode_page.read_text()
        assert "[[topics/very-relevant-topic]]" in text
        assert "[[people/very-relevant-person]]" in text

    def test_existing_links_excluded(self, episode_page, monkeypatch):
        """The page already links to existing-topic and existing-person — those
        must not be re-added even if they're top-cosine matches."""
        _mock_caches(monkeypatch,
                     ["existing-topic", "new-relevant-topic"],
                     ["existing-person", "new-relevant-person"])
        report = enrich.enrich_episode(episode_page)
        assert "existing-topic" not in report["added_topics"]
        assert "existing-person" not in report["added_people"]

    def test_dry_run_does_not_write(self, episode_page, monkeypatch):
        _mock_caches(monkeypatch, ["new-topic", "x"], ["new-person", "y"])
        original = episode_page.read_text()
        report = enrich.enrich_episode(episode_page, dry_run=True)
        assert report["added_topics"] == ["new-topic"]
        assert episode_page.read_text() == original  # unchanged

    def test_max_auto_per_kind_caps_additions(self, episode_page, monkeypatch):
        """Even if many topics score above auto_threshold, only max_auto_per_kind get added."""
        _mock_caches(monkeypatch,
                     ["t1", "t2", "t3", "t4", "t5"],
                     ["p1", "p2", "p3"], dim=8)
        # Force ALL caches to have high cosine via override
        def big_embed(texts, **kw):
            return np.ones((1, 8), dtype=np.float32)
        monkeypatch.setattr(embeddings, "embed_texts", big_embed)

        # Override cached vecs to all-ones too → all cosine = 1.0
        topics_cache = embeddings.EmbedResult(
            ids=np.array(["t1", "t2", "t3", "t4", "t5"], dtype=object),
            vecs=np.ones((5, 8), dtype=np.float32),
            model="test", dim=8,
        )
        people_cache = embeddings.EmbedResult(
            ids=np.array(["p1", "p2", "p3"], dtype=object),
            vecs=np.ones((3, 8), dtype=np.float32),
            model="test", dim=8,
        )
        monkeypatch.setattr(embeddings, "load", lambda n: topics_cache if n == "topics" else people_cache)

        report = enrich.enrich_episode(episode_page, max_auto_per_kind=2)
        assert len(report["added_topics"]) == 2
        assert len(report["added_people"]) == 2

    def test_no_embeddings_cache_is_no_op(self, episode_page, monkeypatch):
        """If the cache files don't exist, return empty report — don't crash."""
        def raise_fnf(name):
            raise FileNotFoundError(name)
        monkeypatch.setattr(embeddings, "load", raise_fnf)
        report = enrich.enrich_episode(episode_page)
        assert report == {"added_topics": [], "added_people": [],
                          "suggested_topics": [], "suggested_people": []}


class TestFindNeighbors:
    def test_excludes_set_filtered_out(self):
        cached = embeddings.EmbedResult(
            ids=np.array(["a", "b", "c", "d"], dtype=object),
            vecs=np.eye(4, dtype=np.float32),
            model="m", dim=4,
        )
        # Query aligned with "a" gets cosine 1.0 with a, 0.0 with others
        q = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        result = enrich.find_neighbors(q, cached, exclude={"a"}, top_k=3)
        slugs = [s for s, _ in result]
        assert "a" not in slugs
        assert len(result) == 3

    def test_returns_descending_score(self):
        cached = embeddings.EmbedResult(
            ids=np.array(["x", "y", "z"], dtype=object),
            vecs=np.array([[1, 0, 0], [0.5, 0.5, 0], [0, 0, 1]], dtype=np.float32),
            model="m", dim=3,
        )
        q = np.array([[1.0, 0, 0]], dtype=np.float32)
        result = enrich.find_neighbors(q, cached, top_k=3)
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)
