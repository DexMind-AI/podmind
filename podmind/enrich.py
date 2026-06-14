"""Embedding-based cross-link enrichment for wiki episode pages.

When a new episode is ingested, the LLM picks ~5-8 topics and ~5-8 people
to link to. It often invents new slugs (`iran-nuclear`) for concepts that
already have established pages (`iran-nuclear-program` with 73 citations),
or simply misses semantically-relevant existing pages.

This module embeds the episode's hook+takeaways and surfaces the top-K
most-similar EXISTING topic and people pages by cosine. Results above
`auto_threshold` get added to the page's cross-links automatically;
results in the `suggest_threshold..auto_threshold` band get logged for
human review without auto-adding.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from podmind import embeddings, paths
from podmind.frontmatter import parse_file


AUTO_THRESHOLD = 0.55     # cosine ≥ this → auto-add the cross-link
SUGGEST_THRESHOLD = 0.45  # cosine in [suggest, auto) → log to stderr only
MAX_AUTO_PER_KIND = 3     # cap on auto-added topics + auto-added people


def episode_query_text(body: str) -> str:
    """The text we use to embed an episode for enrichment — same extraction
    rule as `bin/embed_all.py::episode_text` so the cosine geometry is
    consistent with the cached corpus."""
    if not body.strip():
        return ""
    lines = body.splitlines()
    hook = next((l.strip() for l in lines if l.strip()), "")
    takeaways: list[str] = []
    capture = False
    for line in lines:
        if line.startswith("## Key takeaways"):
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture:
            takeaways.append(line)
    parts = [hook]
    if takeaways:
        parts.append("\n".join(takeaways).strip())
    return "\n\n".join(p for p in parts if p)


def find_neighbors(
    query_vec: np.ndarray,
    cached: embeddings.EmbedResult,
    *,
    exclude: set[str] | None = None,
    top_k: int = 12,
) -> list[tuple[str, float]]:
    """Return [(slug, cosine), ...] of the top-K neighbors not in `exclude`."""
    if cached.vecs.size == 0:
        return []
    sims = embeddings.cosine_similarity(query_vec, cached.vecs)[0]
    order = np.argsort(-sims)
    out: list[tuple[str, float]] = []
    excl = exclude or set()
    for i in order:
        slug = str(cached.ids[i])
        if slug in excl:
            continue
        out.append((slug, float(sims[i])))
        if len(out) >= top_k:
            break
    return out


def enrich_episode(
    page: Path,
    *,
    auto_threshold: float = AUTO_THRESHOLD,
    suggest_threshold: float = SUGGEST_THRESHOLD,
    max_auto_per_kind: int = MAX_AUTO_PER_KIND,
    dry_run: bool = False,
) -> dict:
    """Enrich one episode page in place.

    Returns a report dict:
      {
        "added_topics": [str, ...],
        "added_people": [str, ...],
        "suggested_topics": [(slug, cosine), ...],
        "suggested_people": [(slug, cosine), ...],
      }
    """
    fm, body = parse_file(page)
    query = episode_query_text(body)
    report = {"added_topics": [], "added_people": [],
              "suggested_topics": [], "suggested_people": []}
    if not query:
        return report

    # Existing cross-links — parse from the body's `Cross-links` section
    existing_topics = set(re.findall(r"\[\[topics/([^\]|]+)\]\]", body))
    existing_people = set(re.findall(r"\[\[people/([^\]|]+)\]\]", body))

    try:
        topics_cache = embeddings.load("topics")
        people_cache = embeddings.load("people")
    except FileNotFoundError:
        return report

    q_vec = embeddings.embed_texts([query], model=topics_cache.model)

    # Topics
    topic_neighbors = find_neighbors(q_vec, topics_cache, exclude=existing_topics, top_k=12)
    for slug, score in topic_neighbors:
        if score >= auto_threshold and len(report["added_topics"]) < max_auto_per_kind:
            report["added_topics"].append(slug)
        elif score >= suggest_threshold:
            report["suggested_topics"].append((slug, score))

    # People
    people_neighbors = find_neighbors(q_vec, people_cache, exclude=existing_people, top_k=8)
    for slug, score in people_neighbors:
        if score >= auto_threshold and len(report["added_people"]) < max_auto_per_kind:
            report["added_people"].append(slug)
        elif score >= suggest_threshold:
            report["suggested_people"].append((slug, score))

    if not dry_run and (report["added_topics"] or report["added_people"]):
        _append_to_cross_links(page, body, report["added_topics"], report["added_people"])

    return report


def _append_to_cross_links(page: Path, body: str, new_topics: list[str], new_people: list[str]) -> None:
    """Insert the new slugs into the existing `## Cross-links` block."""
    text = page.read_text()

    if new_people:
        text = _extend_or_create_line(text, "People:", "people", new_people)
    if new_topics:
        text = _extend_or_create_line(text, "Topics:", "topics", new_topics)

    page.write_text(text)


def _extend_or_create_line(text: str, label: str, kind: str, new_slugs: list[str]) -> str:
    """Append to an existing `- People: [[...]], [[...]]` line, or create one."""
    pattern = re.compile(rf"^- {re.escape(label)}.*$", re.M)
    new_links = ", ".join(f"[[{kind}/{s}]]" for s in new_slugs)
    m = pattern.search(text)
    if m:
        return text[:m.end()] + ", " + new_links + text[m.end():]
    # No existing line — append under `## Cross-links` (create section if missing)
    if "## Cross-links" not in text:
        text = text.rstrip() + "\n\n## Cross-links\n\n"
    text = text.rstrip() + f"\n- {label} {new_links}\n"
    return text
