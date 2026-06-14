"""Embed wiki content via a provider-agnostic OpenAI-compatible API.

Configuration (highest priority first):
  env: PODMIND_EMBED_BASE_URL, PODMIND_EMBED_MODEL, PODMIND_EMBED_API_KEY
  secrets: embed_base_url, embed_model, embed_api_key
  legacy env: OPENROUTER_API_KEY (still honored for backward compat)
  default: OpenRouter + openai/text-embedding-3-small

Cache layout (per-vault):
    $PODMIND_DATA_ROOT/wiki/.embeddings/
        topics.npz       — {ids: ndarray[str], vecs: ndarray[float32, (N, D)]}
        people.npz
        episodes.npz
        meta.json        — {model, dim, generated_at}
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

from podmind import paths
from podmind.llm import resolve_embed_config

DEFAULT_BATCH = 96  # OpenAI hard cap is 2048 inputs/request, but smaller = better failure granularity

EMBEDDINGS_DIR = paths.WIKI_DIR / ".embeddings"


@dataclass(frozen=True)
class EmbedResult:
    ids: np.ndarray  # shape (N,), dtype object (strings)
    vecs: np.ndarray  # shape (N, D), dtype float32
    model: str
    dim: int


def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    batch: int = DEFAULT_BATCH,
    client: httpx.Client | None = None,
) -> np.ndarray:
    """Embed a list of strings. Returns ndarray shape (len(texts), dim).

    Empty strings are replaced with a single space (OpenAI rejects empty inputs).
    Retries on transient errors with exponential backoff; raises on persistent
    failure after 3 attempts.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    cfg = resolve_embed_config()
    model = model or cfg.model
    cleaned = [t.strip() or " " for t in texts]
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=120)
    try:
        out: list[list[float]] = []
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers; harmless on other providers.
            "HTTP-Referer": "https://github.com/DexMind-AI/podmind",
            "X-Title": "podmind embeddings",
        }
        for start in range(0, len(cleaned), batch):
            chunk = cleaned[start:start + batch]
            for attempt in range(3):
                try:
                    r = client.post(
                        f"{cfg.base_url}/embeddings",
                        headers=headers,
                        json={"model": model, "input": chunk},
                    )
                    r.raise_for_status()
                    data = r.json()["data"]
                    # OpenRouter preserves input order in `data`.
                    out.extend(d["embedding"] for d in sorted(data, key=lambda d: d["index"]))
                    break
                except (httpx.HTTPError, KeyError) as e:
                    if attempt == 2:
                        raise RuntimeError(f"embed_texts failed at chunk {start}: {e}") from e
                    time.sleep(2 ** attempt)
        return np.asarray(out, dtype=np.float32)
    finally:
        if own_client:
            client.close()


def save(name: str, ids: list[str], vecs: np.ndarray, *, model: str) -> Path:
    """Persist embeddings to `$WIKI_DIR/.embeddings/<name>.npz`."""
    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    out = EMBEDDINGS_DIR / f"{name}.npz"
    np.savez_compressed(out, ids=np.asarray(ids, dtype=object), vecs=vecs)
    meta_path = EMBEDDINGS_DIR / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta[name] = {
        "model": model,
        "dim": int(vecs.shape[1]) if vecs.size else 0,
        "count": len(ids),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return out


def load(name: str) -> EmbedResult:
    """Load embeddings previously saved by `save`."""
    npz = np.load(EMBEDDINGS_DIR / f"{name}.npz", allow_pickle=True)
    meta_path = EMBEDDINGS_DIR / "meta.json"
    meta = json.loads(meta_path.read_text())[name] if meta_path.exists() else {}
    return EmbedResult(
        ids=npz["ids"],
        vecs=npz["vecs"],
        model=meta.get("model", ""),
        dim=meta.get("dim", int(npz["vecs"].shape[1]) if npz["vecs"].size else 0),
    )


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity matrix between rows of `a` and rows of `b`.

    Both must be L2-normalizable (no all-zero rows). Returns shape (len(a), len(b)).
    """
    a_n = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-12)
    b_n = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-12)
    return a_n @ b_n.T


def top_k_neighbors(
    vecs: np.ndarray, k: int = 10, exclude_self: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """For each row, return indices and scores of top-k most similar OTHER rows.

    Returns (idx, score) both of shape (N, k).
    """
    sims = cosine_similarity(vecs, vecs)
    if exclude_self:
        np.fill_diagonal(sims, -np.inf)
    idx = np.argpartition(-sims, kth=k - 1, axis=1)[:, :k]
    # Sort the k indices by similarity desc
    rows = np.arange(sims.shape[0])[:, None]
    sorted_order = np.argsort(-sims[rows, idx], axis=1)
    idx = idx[rows, sorted_order]
    scores = sims[rows, idx]
    return idx, scores
