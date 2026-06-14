#!/usr/bin/env -S uv run python
"""Semantic query over embedded wiki content; optionally synthesize → wiki/synthesis/.

Two modes:
  retrieve only — embed the question, print top-K candidate pages by cosine
  synthesize    — additionally call the configured LLM provider to write a synthesis
                  page to `$WIKI/synthesis/<slug>.md` and link it from index.md

Foundation for the `query` write-path in CLAUDE-vault.md — Karpathy's pattern
of "every non-trivial query files an artefact back into the wiki."

Usage:
    ./bin/query.py "what did I learn about Russia from skeptics?"
    ./bin/query.py "claude code skills best practices" --top 30
    ./bin/query.py "what's the bull case for Berachain?" --synthesize
    ./bin/query.py "..." --corpus people --top 15

Cost when --synthesize: ~$0.005/query (measured on DeepSeek V4 reading 20 page snippets).
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

from podmind import embeddings, llm
from podmind.frontmatter import parse_file
from _lib import WIKI_DIR, SECRETS

SYNTH_PROMPT = """You are synthesizing a knowledge-wiki page from podcast episode summaries.

User's question:
{question}

You have access to the following candidate pages (top-{k} by semantic similarity to the question). Each is a topic page with citations to the underlying episodes. Read them, then write a synthesis that directly answers the question.

Output format (markdown):
  - Start with a 2-3 sentence direct answer.
  - Then 4-8 bullets of supporting evidence, each citing `[[episodes/<slug>]]` from the candidate pages where relevant.
  - End with a `## Gaps` section listing what isn't well-covered (1-3 bullets) — this surfaces what to ingest next.

Don't invent claims. If the candidates don't answer the question, say so directly and use Gaps to recommend what to listen to.

Candidates:
{candidates}
"""


def _slugify_question(q: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
    return s[:60]


def _candidate_text(page: Path, max_chars: int = 800) -> str:
    """Compact representation of a topic/people/episode page for the prompt."""
    if not page.exists():
        return ""
    _, body = parse_file(page)
    if not body.strip():
        body = page.read_text(errors="ignore")
    return body.strip()[:max_chars]


def synthesize(question: str, candidates: list[tuple[str, float, Path]],
               provider: llm.LLMProvider) -> str:
    """Call the configured LLM to generate a synthesis from the question + top candidates."""
    block = []
    for i, (slug, score, page) in enumerate(candidates, 1):
        snippet = _candidate_text(page)
        block.append(f"### {i}. [[topics/{slug}]] (cosine={score:.2f})\n\n{snippet}\n")
    prompt = SYNTH_PROMPT.format(question=question, k=len(candidates), candidates="\n".join(block))

    try:
        content, _ = provider.chat(prompt, json_mode=False, temperature=0.3,
                                   max_tokens=2000, timeout=120)
    except llm.LLMError as e:
        raise RuntimeError(f"LLM synthesis failed: {e}") from e
    return content.strip()


def file_synthesis(question: str, content: str, sources: list[str]) -> Path:
    """Write the synthesis to wiki/synthesis/<slug>.md with frontmatter + log entry."""
    slug = _slugify_question(question)
    today = datetime.now().strftime("%Y-%m-%d")
    out = WIKI_DIR / "synthesis" / f"{slug}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = (
        "---\n"
        f"synthesized: {today}\n"
        f"question: {json.dumps(question)}\n"
        f"sources_searched: topics ({len(sources)} candidates)\n"
        "---\n\n"
    )
    body = f"# {question}\n\n{content}\n"
    out.write_text(frontmatter + body)

    # Append log entry
    log = WIKI_DIR / "log.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (
        f"\n## [{ts}] query\n"
        f"- question: {json.dumps(question)}\n"
        f"- searched: topics layer ({len(sources)} candidates by cosine)\n"
        f"- synthesis filed: `synthesis/{slug}.md`\n"
        f"- top sources: {', '.join(f'[[{s}]]' for s in sources[:5])}\n"
    )
    log.write_text(log.read_text() + block)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="natural-language question")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--corpus", default="topics", choices=["topics", "people", "episodes", "synthesis"])
    ap.add_argument("--synthesize", action="store_true",
                    help="call the configured LLM provider to write a synthesis to wiki/synthesis/")
    args = ap.parse_args()

    # Resolve LLM provider early (fail fast on bad key) even if --synthesize
    # isn't set, so embedding failures don't hide a missing key.
    provider = llm.get_provider()

    try:
        cached = embeddings.load(args.corpus)
    except FileNotFoundError:
        print(f"No embeddings cache for '{args.corpus}'. Run `./bin/embed_all.py` first.")
        sys.exit(1)

    q_vec = embeddings.embed_texts([args.query], model=cached.model)
    sims = embeddings.cosine_similarity(q_vec, cached.vecs)[0]
    top_idx = np.argsort(-sims)[:args.top]

    candidates: list[tuple[str, float, Path]] = []
    print(f"\nTop {args.top} {args.corpus} for: \"{args.query}\"\n")
    for i in top_idx:
        slug = str(cached.ids[i])
        score = float(sims[i])
        page = WIKI_DIR / args.corpus / f"{slug}.md"
        n_cites = page.read_text(errors="ignore").count("[[episodes/") if page.exists() else 0
        print(f"  {score:.3f}  [[{args.corpus}/{slug}]]  ({n_cites} citations)")
        candidates.append((slug, score, page))

    if args.synthesize:
        print(f"\nsynthesizing via {provider.cfg.model}...")
        content = synthesize(args.query, candidates, provider)
        sources = [f"{args.corpus}/{slug}" for slug, _, _ in candidates]
        out = file_synthesis(args.query, content, sources)
        print(f"\n→ filed {out}")
        print("---")
        print(content[:600])
        if len(content) > 600:
            print(f"... [{len(content)-600} more chars in file]")


if __name__ == "__main__":
    main()
