"""LLM-driven thread clusterer.

Takes a window of episodes + the bridges (cross-cutting people/topics)
from `threads.py` and asks the configured LLM to identify 3–6 narrative
threads.

The LLM does the interpretive work: bridges are mechanical seeds, but
"Merz coalition crisis" is a name only a model can produce. We parse
strict JSON output; on parse failure we retry once, then fall back to
bridge-driven mechanical clustering so the digest pipeline never breaks.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Callable

from podmind import llm
from podmind.frontmatter import EpisodePage
from podmind.llm_json import extract_json as _extract_json
from podmind.threads import Thread

# MiniMax (and other reasoning models) emit a long <think> block before the
# JSON. With a tight ceiling the JSON gets truncated and parsing fails, which
# silently degrades the digest to the bridge fallback. Mirror the summarize
# path's budget (bin/summarize.py MAX_OUTPUT_TOKENS) so the JSON survives.
MAX_THREAD_TOKENS = int(os.environ.get("PODMIND_MAX_OUTPUT_TOKENS", "16000"))


PROMPT_TEMPLATE = """You are clustering one user's recent podcast listening into 3–6 thematic threads. A thread is a coherent through-line that several episodes contribute to.

INPUTS

Episodes (one block per episode, separated by `---`):
{episodes_block}

Cross-cutting bridges (people/topics referenced by ≥2 episodes — strong seeds for thread identification):
{bridges_block}

OUTPUT

Return a single JSON object. No markdown fences, no prose before or after. Schema:

{{
  "threads": [
    {{
      "name": "Short narrative title (≤7 words). NOT a slug. NOT just a person name unless the person IS the thread. Examples of good titles: 'Merz coalition crisis', 'Crypto regulation push', 'Putin's diplomatic ceiling'. Bad: 'friedrich-merz', 'german politics'.",
      "summary": "One sentence (≤30 words) on what the through-line is and why these episodes belong together.",
      "episode_slugs": ["slug1", "slug2", "..."]
    }}
  ],
  "uncategorized": ["slug-of-orphan-ep", "..."]
}}

RULES

- Every episode in the input must appear in exactly one thread OR in `uncategorized`. No duplicates, no omissions.
- A thread MUST have ≥2 episodes. One-off episodes go to `uncategorized`.
- 3–6 threads is the target; fewer is OK on a sparse day. Don't pad.
- Use bridges as seeds but feel free to span beyond them — narrative threads aren't limited to a single shared entity.
- Order `threads` from most-episodes to fewest.
- Use the episode slugs EXACTLY as given. Do not paraphrase or shorten.
"""


def format_episodes_for_prompt(eps_with_slugs: list[tuple[str, EpisodePage]]) -> str:
    """Compact representation: slug + show + date + hook. Keep it short so
    we can fit a typical 30-50 episode window in the context window
    without blowing past max_tokens."""
    blocks: list[str] = []
    for slug, ep in eps_with_slugs:
        block = f"slug: {slug}\nshow: {ep.show or '?'}\ndate: {ep.date or '?'}\nhook: {ep.hook}"
        blocks.append(block)
    return "\n---\n".join(blocks)


def format_bridges_for_prompt(person_bridges: dict[str, list[str]],
                              topic_bridges: dict[str, list[str]]) -> str:
    """Render bridges as a hint section. Sort by degree desc so the LLM
    sees the strongest seeds first."""
    lines: list[str] = []
    items = [(f"person/{p}", eps) for p, eps in person_bridges.items()]
    items += [(f"topic/{t}", eps) for t, eps in topic_bridges.items()]
    items.sort(key=lambda kv: -len(kv[1]))
    for name, eps in items[:30]:  # cap at 30 strongest bridges
        lines.append(f"- {name}: {len(eps)} episodes ({', '.join(eps[:5])}{'...' if len(eps) > 5 else ''})")
    return "\n".join(lines) if lines else "(no bridges — every entity appears in only one episode this window)"


def parse_llm_response(text: str, valid_slugs: set[str]) -> tuple[list[Thread], list[str]]:
    """Parse LLM JSON → (threads, uncategorized).

    Drops any episode_slugs the LLM hallucinated (not in valid_slugs).
    Drops any threads that fall below 2 episodes after filtering. Those
    orphaned episodes go to uncategorized so they aren't lost.
    """
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("no JSON object in LLM response")
    raw_threads = data.get("threads", [])
    raw_uncat = list(data.get("uncategorized", []))

    threads: list[Thread] = []
    seen: set[str] = set()
    for rt in raw_threads:
        name = (rt.get("name") or "").strip()
        summary = (rt.get("summary") or "").strip()
        slugs = [s for s in rt.get("episode_slugs", []) if s in valid_slugs and s not in seen]
        if not name or len(slugs) < 2:
            # Demote: any kept-but-now-orphan slugs go to uncategorized
            raw_uncat.extend(slugs)
            continue
        threads.append(Thread(name=name, summary=summary, episode_slugs=slugs))
        seen.update(slugs)

    # Anything in valid_slugs not yet placed → uncategorized
    missing = [s for s in valid_slugs if s not in seen and s not in raw_uncat]
    raw_uncat.extend(missing)
    # Dedupe + filter to valid
    uncat = []
    uc_seen: set[str] = set()
    for s in raw_uncat:
        if s in valid_slugs and s not in seen and s not in uc_seen:
            uncat.append(s)
            uc_seen.add(s)

    return threads, uncat


def synthesize_threads(
    eps_with_slugs: list[tuple[str, EpisodePage]],
    person_bridges: dict[str, list[str]],
    topic_bridges: dict[str, list[str]],
    *,
    timeout: float = 120.0,
    chat_fn: Callable[[str], tuple[str, llm.Usage]] | None = None,
) -> tuple[list[Thread], list[str]]:
    """Call the configured LLM to cluster episodes into threads.

    `chat_fn` is a testability seam — defaults to the real provider call.
    Signature: ``(prompt: str) -> (content: str, usage: llm.Usage)``.

    Returns (threads, uncategorized_slugs). On LLM failure, returns
    mechanical fallback: one thread per top bridge, everything else
    uncategorized. The digest pipeline never breaks because of this call.
    """
    valid_slugs = {slug for slug, _ in eps_with_slugs}
    if len(eps_with_slugs) < 2:
        # Single episode or empty — no clustering to do.
        return [], [slug for slug, _ in eps_with_slugs]

    prompt = PROMPT_TEMPLATE.format(
        episodes_block=format_episodes_for_prompt(eps_with_slugs),
        bridges_block=format_bridges_for_prompt(person_bridges, topic_bridges),
    )

    if chat_fn is None:
        try:
            provider = llm.get_provider()
        except Exception as e:
            return _mechanical_fallback(eps_with_slugs, person_bridges, topic_bridges,
                                        reason=f"provider init failed: {e}")

        def chat_fn(p: str) -> tuple[str, llm.Usage]:
            return provider.chat(p, json_mode=True, temperature=0.4,
                                 max_tokens=MAX_THREAD_TOKENS, timeout=timeout)

    has_bridges = bool(person_bridges or topic_bridges)
    last_error = ""
    for attempt in range(2):
        try:
            content, _ = chat_fn(prompt)
            threads, uncat = parse_llm_response(content, valid_slugs)
        except (llm.LLMError, json.JSONDecodeError, ValueError, KeyError) as e:
            last_error = f"{type(e).__name__}: {str(e)[:200]}"
            continue
        # A valid response with real threads — done. Also accept an empty result
        # when there's nothing to thread (no cross-cutting entities this window).
        if threads or not has_bridges:
            return threads, uncat
        # Parsed OK but 0 threads despite shared entities — a known stochastic
        # miss from the model. Retry once for narrative threads; if the retry is
        # also empty we fall through to entity grouping rather than ship an
        # all-uncategorized digest.
        last_error = "LLM returned 0 threads despite shared entities"
    return _mechanical_fallback(eps_with_slugs, person_bridges, topic_bridges,
                                reason=last_error)


def _entity_name(slug: str) -> str:
    """Human-readable name from an entity slug: 'elon-musk' → 'Elon Musk'."""
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def _mechanical_fallback(
    eps_with_slugs: list[tuple[str, EpisodePage]],
    person_bridges: dict[str, list[str]],
    topic_bridges: dict[str, list[str]],
    *,
    reason: str,
) -> tuple[list[Thread], list[str]]:
    """When the LLM fails, group episodes by their strongest shared entity so
    the digest still reads cleanly. Produces presentable, user-facing thread
    names — the failure `reason` is logged, never shown in the digest.
    """
    # Observability without leaking internals into the reader-facing digest.
    print(f"threads: LLM synthesis unavailable ({reason}); "
          f"grouping by shared entity", file=sys.stderr)

    valid_slugs = {slug for slug, _ in eps_with_slugs}
    placed: set[str] = set()
    threads: list[Thread] = []

    candidates = (
        [(_entity_name(p), eps) for p, eps in person_bridges.items()] +
        [(_entity_name(t), eps) for t, eps in topic_bridges.items()]
    )
    candidates.sort(key=lambda kv: -len(kv[1]))
    for name, eps in candidates[:5]:
        new_eps = [e for e in eps if e in valid_slugs and e not in placed]
        if len(new_eps) >= 2:
            threads.append(Thread(
                name=name,
                summary=f"Episodes connected by {name}.",
                episode_slugs=new_eps,
            ))
            placed.update(new_eps)

    uncat = [slug for slug in valid_slugs if slug not in placed]
    return threads, uncat
