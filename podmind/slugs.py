"""Slug tokenization shared by lint, merge, and any future analyzer.

A slug like `iran-nuclear-program` decomposes into tokens that we use for
similarity scoring (jaccard) when looking for near-duplicate topic pages.

The stopword list filters pure connective tissue — `and`, `the`, `vs` etc. —
so `bitcoin-vs-ethereum` and `bitcoin-and-ethereum` collapse to the same set
{bitcoin, ethereum} and score as identical topics. Without filtering, those
joiners would inflate the union and hide real duplicates.

Avoiding the trap: do NOT also filter short tokens by length. Doing so drops
meaningful 2-char domain abbreviations (`ai`, `eu`, `us`, `ev`, `ml`) and
causes false positives like `economics` ≡ `ev-economics` (both reduce to
{economics} after the `ev` 2-char drop).
"""
from __future__ import annotations

SLUG_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "in", "on", "and", "or", "to", "vs",
    "by", "for", "with", "from", "as", "is", "at",
})


def slug_tokens(slug: str) -> frozenset[str]:
    """Return the set of meaningful tokens in `slug` (split on `-`).

    Pure connective stopwords are filtered. All other tokens are kept,
    regardless of length — short domain markers like `ai`, `eu`, `us` matter
    for distinguishing topics.
    """
    return frozenset(t for t in slug.split("-") if t and t not in SLUG_STOPWORDS)
