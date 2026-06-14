#!/usr/bin/env -S uv run python
"""Embed every corpus that powers semantic search → wiki/.embeddings/*.npz

Four corpora, each with a tailored text-extraction strategy:

  topics      — `<slug-as-prose>\\n\\n<first 500 chars of body>`
  people      — `<name>\\n\\n<bio + role>` (skips pages that are pure citations)
  episodes    — `<hook>\\n\\n<takeaways list>` — the meaty part, not the cross-links
  synthesis   — `<title>\\n\\n<first 2000 chars of body>`

Each corpus caches independently so a daily incremental run only embeds
genuinely-new pages. Total cost on a fresh cache for the current corpus
is ~$0.10 at openai/text-embedding-3-small via OpenRouter.

Usage:
    ./bin/embed_all.py                    # incremental — only new pages
    ./bin/embed_all.py --full             # re-embed everything (after model swap)
    ./bin/embed_all.py --only topics      # one corpus only
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

from podmind import embeddings
from podmind.frontmatter import EpisodePage, parse_file
from podmind.llm import resolve_embed_config
from _lib import WIKI_DIR


# ---- per-corpus text extraction ---------------------------------------------

def topic_text(slug: str, body: str) -> str | None:
    label = slug.replace("-", " ")
    snippet = body.strip()[:500] if body else ""
    return f"{label}\n\n{snippet}" if snippet else label


def person_text(slug: str, body: str) -> str | None:
    """People pages: `# Name\\n\\n_role._\\n\\nbio\\n\\n## Citations\\n- ...`."""
    if not body.strip():
        return slug.replace("-", " ")
    # Drop the "## Citations" tail; it's just back-references that are noisy.
    head = body.split("## Citations", 1)[0].strip()
    if not head:
        head = body.strip()
    return head[:800]


def episode_text(slug: str, body: str) -> str | None:
    """Episode pages: hook line + key-takeaways block. Skip cross-links."""
    if not body.strip():
        return None
    lines = body.splitlines()
    hook = ""
    for line in lines:
        if line.strip():
            hook = line.strip()
            break
    # Pull the "## Key takeaways" block until the next header or end.
    takeaway_section = []
    capture = False
    for line in lines:
        if line.startswith("## Key takeaways"):
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture:
            takeaway_section.append(line)
    takeaways = "\n".join(takeaway_section).strip()
    parts = [hook]
    if takeaways:
        parts.append(takeaways)
    return "\n\n".join(parts) or hook or slug.replace("-", " ")


def synthesis_text(slug: str, body: str) -> str | None:
    if not body.strip():
        return slug.replace("-", " ")
    return body.strip()[:2000]


CORPORA = {
    "topics": ("topics", topic_text),
    "people": ("people", person_text),
    "episodes": ("episodes", episode_text),
    "synthesis": ("synthesis", synthesis_text),
}


# ---- driver -----------------------------------------------------------------

def embed_corpus(name: str, model: str, full: bool) -> None:
    subdir, extractor = CORPORA[name]
    pages = sorted((WIKI_DIR / subdir).glob("*.md"))
    if not pages:
        print(f"  {name}: 0 pages, skipping")
        return

    on_disk_ids = {p.stem for p in pages}
    cached_ids: set[str] = set()
    cached_ids_arr = None
    cached_vecs = None
    if not full:
        try:
            cached = embeddings.load(name)
            cached_ids = set(cached.ids.tolist())
            cached_ids_arr = cached.ids
            cached_vecs = cached.vecs
        except (FileNotFoundError, KeyError):
            pass

    # Drop cached IDs whose wiki page has been deleted/merged away.
    orphaned = cached_ids - on_disk_ids
    kept_indices = None
    if orphaned and cached_ids_arr is not None:
        kept_mask = np.array([str(i) not in orphaned for i in cached_ids_arr])
        cached_ids_arr = cached_ids_arr[kept_mask]
        cached_vecs = cached_vecs[kept_mask]
        cached_ids = set(cached_ids_arr.tolist())

    to_embed: list[tuple[str, str]] = []
    skipped = 0
    for p in pages:
        if p.stem in cached_ids:
            continue
        _, body = parse_file(p)
        if not body.strip():
            body = p.read_text(errors="ignore")
        text = extractor(p.stem, body)
        if not text:
            skipped += 1
            continue
        to_embed.append((p.stem, text))

    print(
        f"  {name}: {len(pages)} pages | cached {len(cached_ids)} | "
        f"new {len(to_embed)} | orphaned-evicted {len(orphaned)} | skipped(empty) {skipped}"
    )
    if not to_embed and not orphaned:
        return

    if to_embed:
        ids = [i for i, _ in to_embed]
        texts = [t for _, t in to_embed]
        new_vecs = embeddings.embed_texts(texts, model=model)
    else:
        ids, new_vecs = [], np.zeros((0, cached_vecs.shape[1] if cached_vecs is not None else 0), dtype=np.float32)

    if cached_ids_arr is not None and cached_vecs is not None and len(cached_ids_arr):
        all_ids = list(cached_ids_arr) + ids
        all_vecs = np.vstack([cached_vecs, new_vecs]) if len(ids) else cached_vecs
    else:
        all_ids, all_vecs = ids, new_vecs

    out = embeddings.save(name, all_ids, all_vecs, model=model)
    print(f"    → {out} ({len(all_ids)} vectors × {all_vecs.shape[1]} dim)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None,
                    help="embedding model (default: from PODMIND_EMBED_MODEL config)")
    ap.add_argument("--full", action="store_true",
                    help="re-embed everything (default is incremental)")
    ap.add_argument("--only", choices=list(CORPORA), action="append",
                    help="restrict to a single corpus (repeatable)")
    args = ap.parse_args()

    model = args.model or resolve_embed_config().model
    targets = args.only or list(CORPORA)
    print(f"embedding {targets} via {model} ({'full' if args.full else 'incremental'})")
    for name in targets:
        embed_corpus(name, model, args.full)
    print("done")


if __name__ == "__main__":
    main()
