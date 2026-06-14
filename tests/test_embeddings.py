"""Tests for podmind.embeddings.

HTTP is mocked end-to-end; no API key needed. We validate:
  - batch chunking
  - retry logic
  - cosine similarity correctness
  - top-k neighbor ranking
  - cache roundtrip
"""
import os
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from podmind import embeddings


@pytest.fixture
def mock_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _mock_response(input_texts: list[str], dim: int = 4) -> MagicMock:
    """Build a fake OpenRouter response shape."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "data": [
            {"index": i, "embedding": [float(i + j * 0.1) for j in range(dim)]}
            for i in range(len(input_texts))
        ]
    }
    return resp


class TestEmbedTexts:
    def test_empty_input_returns_empty(self, mock_api_key):
        result = embeddings.embed_texts([])
        assert result.shape == (0, 0)

    def test_single_batch(self, mock_api_key):
        client = MagicMock()
        client.post.return_value = _mock_response(["a", "b", "c"])
        result = embeddings.embed_texts(["a", "b", "c"], client=client)
        assert result.shape == (3, 4)
        assert client.post.call_count == 1

    def test_chunked_across_batches(self, mock_api_key):
        client = MagicMock()
        client.post.side_effect = [
            _mock_response(["a"] * 5),
            _mock_response(["b"] * 5),
            _mock_response(["c"] * 2),
        ]
        result = embeddings.embed_texts(["x"] * 12, batch=5, client=client)
        assert result.shape == (12, 4)
        assert client.post.call_count == 3

    def test_empty_strings_replaced_with_space(self, mock_api_key):
        """OpenAI rejects empty input; we substitute a single space."""
        client = MagicMock()
        client.post.return_value = _mock_response(["", " ", "hi"])
        embeddings.embed_texts(["", "  ", "hi"], client=client)
        sent = client.post.call_args.kwargs["json"]["input"]
        assert sent == [" ", " ", "hi"]

    def test_retry_on_transient_failure(self, mock_api_key):
        import httpx
        client = MagicMock()
        client.post.side_effect = [
            httpx.ConnectError("boom"),
            _mock_response(["a", "b"]),
        ]
        result = embeddings.embed_texts(["a", "b"], client=client)
        assert result.shape == (2, 4)
        assert client.post.call_count == 2

    def test_raises_after_3_attempts(self, mock_api_key):
        import httpx
        client = MagicMock()
        client.post.side_effect = httpx.ConnectError("persistent")
        with pytest.raises(RuntimeError, match="embed_texts failed"):
            embeddings.embed_texts(["a"], client=client)

    def test_no_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("PODMIND_EMBED_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            embeddings.embed_texts(["a"])

    def test_honors_podmind_embed_base_url(self, monkeypatch):
        """embed_texts posts to PODMIND_EMBED_BASE_URL when set."""
        monkeypatch.setenv("PODMIND_EMBED_BASE_URL", "https://custom.example.com/v1")
        monkeypatch.setenv("PODMIND_EMBED_API_KEY", "custom-key")
        client = MagicMock()
        client.post.return_value = _mock_response(["hello"])
        embeddings.embed_texts(["hello"], client=client)
        url = client.post.call_args.args[0] if client.post.call_args.args else client.post.call_args.kwargs.get("url") or client.post.call_args[0][0]
        assert url == "https://custom.example.com/v1/embeddings"


class TestCosineSimilarity:
    def test_identical_vectors_score_1(self):
        a = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        sim = embeddings.cosine_similarity(a, a)
        assert sim[0, 0] == pytest.approx(1.0)

    def test_orthogonal_vectors_score_0(self):
        a = np.array([[1.0, 0.0]], dtype=np.float32)
        b = np.array([[0.0, 1.0]], dtype=np.float32)
        assert embeddings.cosine_similarity(a, b)[0, 0] == pytest.approx(0.0)

    def test_opposite_vectors_score_neg1(self):
        a = np.array([[1.0, 0.0]], dtype=np.float32)
        b = np.array([[-1.0, 0.0]], dtype=np.float32)
        assert embeddings.cosine_similarity(a, b)[0, 0] == pytest.approx(-1.0)

    def test_matrix_shape(self):
        a = np.random.rand(5, 3).astype(np.float32)
        b = np.random.rand(7, 3).astype(np.float32)
        assert embeddings.cosine_similarity(a, b).shape == (5, 7)

    def test_handles_zero_vectors_without_nan(self):
        """A row of zeros shouldn't cause NaN."""
        a = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32)
        sim = embeddings.cosine_similarity(a, a)
        assert not np.isnan(sim).any()


class TestTopKNeighbors:
    def test_excludes_self(self):
        """Vectors should not be their own nearest neighbor."""
        v = np.eye(5, dtype=np.float32)
        idx, scores = embeddings.top_k_neighbors(v, k=2, exclude_self=True)
        # Diagonal exclusion means vector i should not appear in idx[i]
        for i in range(5):
            assert i not in idx[i]

    def test_returns_correct_k(self):
        v = np.random.rand(20, 8).astype(np.float32)
        idx, scores = embeddings.top_k_neighbors(v, k=3)
        assert idx.shape == (20, 3)
        assert scores.shape == (20, 3)

    def test_scores_sorted_desc(self):
        """For each row, the returned scores should be in descending order."""
        v = np.random.rand(10, 6).astype(np.float32)
        _, scores = embeddings.top_k_neighbors(v, k=4)
        for row in scores:
            assert (row[:-1] >= row[1:]).all()


class TestCacheRoundtrip:
    def test_save_then_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(embeddings, "EMBEDDINGS_DIR", tmp_path)
        ids = ["topic-a", "topic-b", "topic-c"]
        vecs = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
        embeddings.save("topics", ids, vecs, model="openai/text-embedding-3-small")
        loaded = embeddings.load("topics")
        assert list(loaded.ids) == ids
        assert (loaded.vecs == vecs).all()
        assert loaded.model == "openai/text-embedding-3-small"
        assert loaded.dim == 3

    def test_meta_json_written(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr(embeddings, "EMBEDDINGS_DIR", tmp_path)
        embeddings.save("topics", ["a"], np.zeros((1, 4), dtype=np.float32), model="m")
        meta = json.loads((tmp_path / "meta.json").read_text())
        assert "topics" in meta
        assert meta["topics"]["count"] == 1
        assert meta["topics"]["dim"] == 4
