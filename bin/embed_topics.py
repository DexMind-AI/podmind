#!/usr/bin/env -S uv run python
"""Embed all topic pages → wiki/.embeddings/topics.npz.

Each topic page is embedded by its title (the slug, prettified) plus its
citation context (the first ~500 chars of body). That gives the embedder
both the surface label and the semantic neighborhood the topic actually
covers in this user's listening.

Usage:
    ./bin/embed_topics.py                          # embed everything
    ./bin/embed_topics.py --incremental             # skip already-cached IDs
    ./bin/embed_topics.py --model openai/text-embedding-3-large
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from podmind import embeddings
from podmind.frontmatter import parse_file
from podmind.llm import resolve_embed_config
from _lib import WIKI_DIR


def topic_text(slug: str, body: str) -> str:
    """Build the text to embed: surface form of the slug + body context."""
    label = slug.replace("-", " ")
    snippet = body.strip()[:500] if body else ""
    return f"{label}\n\n{snippet}" if snippet else label


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None,
                    help="embedding model (default: from PODMIND_EMBED_MODEL config)")
    ap.add_argument("--incremental", action="store_true",
                    help="skip topics whose IDs are already in the cache")
    ap.add_argument("--limit", type=int, help="cap (for testing)")
    args = ap.parse_args()

    topics_dir = WIKI_DIR / "topics"
    pages = sorted(topics_dir.glob("*.md"))
    if args.limit:
        pages = pages[:args.limit]
    print(f"found {len(pages)} topic pages")

    cached_ids: set[str] = set()
    cached_ids_arr = None
    cached_vecs = None
    if args.incremental:
        try:
            cached = embeddings.load("topics")
            cached_ids = set(cached.ids.tolist())
            cached_ids_arr = cached.ids
            cached_vecs = cached.vecs
            print(f"  already cached: {len(cached_ids)}")
        except (FileNotFoundError, KeyError):
            print("  no existing cache; embedding everything")

    to_embed: list[tuple[str, str]] = []
    for p in pages:
        if p.stem in cached_ids:
            continue
        _, body = parse_file(p)
        if not body.strip():
            body = p.read_text(errors="ignore")
        to_embed.append((p.stem, topic_text(p.stem, body)))

    if not to_embed:
        print("nothing new to embed; cache is current")
        return

    model = args.model or resolve_embed_config().model
    print(f"embedding {len(to_embed)} new topic pages with {model}")
    ids = [i for i, _ in to_embed]
    texts = [t for _, t in to_embed]
    new_vecs = embeddings.embed_texts(texts, model=model)

    # Merge with cache if present
    if cached_ids_arr is not None and cached_vecs is not None:
        import numpy as np
        all_ids = list(cached_ids_arr) + ids
        all_vecs = np.vstack([cached_vecs, new_vecs])
    else:
        all_ids, all_vecs = ids, new_vecs

    out = embeddings.save("topics", all_ids, all_vecs, model=model)
    print(f"→ wrote {out} ({len(all_ids)} vectors × {all_vecs.shape[1]} dim)")


if __name__ == "__main__":
    main()
