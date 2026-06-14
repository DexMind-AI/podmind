#!/usr/bin/env -S uv run python
"""summarize — reads a tick_prep.py JSON dispatch file and calls the configured
LLM provider per episode, writing per-idx JSON to /tmp/podmind-results/ in the
schema tick_finalize.py expects.

Usage: ./bin/summarize.py /tmp/tick.json [--concurrency 8]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from _lib import DATA_ROOT  # noqa: E402

from podmind import llm
from podmind.llm_json import extract_json

ROOT = DATA_ROOT
RESULTS = Path("/tmp/podmind-results")
RESULTS.mkdir(exist_ok=True)

PROMPT = """You are extracting a structured summary of a podcast episode for a knowledge wiki.

Read the meta and transcript below. Produce a single JSON object with these exact fields. Do not wrap in markdown fences. Do not include any text before or after the JSON.

If the transcript appears corrupt (e.g. wrong language vs episode title, anime dialogue, unrelated content from yt-dlp ytsearch1 mismatch), set guests=[], takeaways=[], quotes=[], people=[], topics=[] and put 'transcript appears corrupt: <one-line reason>' in `hook`.

Schema:
{
  "raw_dir": "<as given>",
  "show_slug": "<as given>",
  "show_name": "<from meta.show>",
  "date": "<YYYY-MM-DD as given>",
  "title": "<from meta.title>",
  "title_slug": "<lowercase kebab-case ≤50 chars>",
  "guests": ["<guest names, exclude host>"],
  "listened": <bool from meta>,
  "played_up_to": <int from meta>,
  "duration_min": <round(meta.duration/60) or 0>,
  "transcript_source": "<from meta>",
  "hook": "<one-sentence summary, ≤140 chars>",
  "takeaways": ["<5–8 substantive bullet strings>"],
  "quotes": [{"text": "<verbatim>", "attribution": "<speaker>", "timestamp": "<HH:MM:SS or null>"}],
  "people": [{"slug": "<kebab>", "name": "<full>", "role": "<host/guest/mentioned>", "note": "<one line>"}],
  "topics": [{"slug": "<kebab>", "name": "<topic>", "why": "<one line, why this episode is relevant>"}]
}

Aim for 5–8 takeaways, 2–3 quotes, 5–8 people, 5–8 topics on full-length episodes. Scale down for short clips (<10KB transcript) — minimum 3 takeaways, 1 quote, 3 people, 3 topics.

**Names — never invent.** Only include a person in `people` if their name appears verbatim in the transcript or meta. If a host is referred to only by a first name or honorific, write the partial form (e.g. name = "Philipp (host)") rather than guess a surname. If you cannot identify the host at all, omit them — do NOT confabulate a plausible-sounding name. The same rule applies to companies, products, places: extract only what's explicitly named. Hallucinated proper nouns are a worse failure mode than missing ones because they pollute the wiki with bad slugs that are hard to detect later.

---

raw_dir: {raw_dir}
show_slug: {show_slug}
date: {date}

meta.json:
{meta_json}

transcript.md:
{transcript}
"""


def build_prompt(item: dict) -> str:
    raw_dir = item["raw_dir"]
    epdir = ROOT / "raw/episodes" / raw_dir
    meta = (epdir / "meta.json").read_text()
    transcript_path = epdir / "transcript.md"
    transcript = transcript_path.read_text() if transcript_path.exists() else "(missing)"
    # Cap monster transcripts at ~400KB (~100k tokens) to stay within context
    if len(transcript) > 400_000:
        head = transcript[:200_000]
        tail = transcript[-150_000:]
        transcript = head + "\n\n[... truncated for context ...]\n\n" + tail
    show_slug, ep_dir_name = raw_dir.split("/", 1)
    date = ep_dir_name[:10] if ep_dir_name[:4].isdigit() else ""
    item.setdefault("show_slug", show_slug)
    item.setdefault("date", date)
    # Substitute the small trusted values into the TEMPLATE first, then
    # splice the big untrusted blobs (meta, transcript) in via partition so
    # their content is never scanned for placeholders. The previous chained
    # .replace() would double-substitute if a transcript contained a literal
    # "{date}" / "{transcript}" (programming podcasts discuss template
    # syntax!) — corrupting the prompt → malformed LLM output → episode
    # silently skipped → re-summarized at cost daily. Review 2026-06-12.
    template = (
        PROMPT
        .replace("{raw_dir}", raw_dir)
        .replace("{show_slug}", show_slug)
        .replace("{date}", date)
    )
    head, _, rest = template.partition("{meta_json}")
    mid, _, tail = rest.partition("{transcript}")
    return head + meta + mid + transcript + tail


async def process_one(client: httpx.AsyncClient, provider: llm.LLMProvider, item: dict,
                      sem: asyncio.Semaphore) -> tuple[str, bool, llm.Usage]:
    async with sem:
        prompt = build_prompt(item)
        try:
            content, usage = await provider.achat(client, prompt)
        except llm.LLMError as e:
            print(f"  [error] {item['raw_dir']}: {e}", file=sys.stderr)
            return item["idx"], False, llm.Usage()
        result = extract_json(content)
        if not isinstance(result, dict):
            print(f"  [error] {item['raw_dir']}: unparseable JSON", file=sys.stderr)
            return item["idx"], False, usage
        result["raw_dir"] = item["raw_dir"]
        result["show_slug"] = item["show_slug"]
        result["date"] = item["date"]
        (RESULTS / f"{item['idx']}.json").write_text(json.dumps(result, indent=2))
        return item["idx"], True, usage


async def main_async(tick_file: str, concurrency: int) -> None:
    dispatch = json.loads(Path(tick_file).read_text())["dispatch"]
    cfg = llm.resolve_chat_config()
    provider = llm.get_provider(cfg)
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        tasks = [process_one(client, provider, item, sem) for item in dispatch]
        results = await asyncio.gather(*tasks)
    ok = sum(1 for _, success, _ in results if success)
    failed = [idx for idx, success, _ in results if not success]
    total = sum((u for _, _, u in results), llm.Usage())
    cost = llm.cost_usd(cfg.model, total)
    # Always also write a sidecar file — stdout buffering can swallow these
    # lines when the parent process is killed before its final flush, as
    # happened on the 2026-06-02 2000-episode run.
    sidecar = RESULTS / "_cost.json"
    sidecar.write_text(json.dumps({
        "model": cfg.model,
        "episodes_ok": ok, "episodes_failed": len(failed),
        "input_tokens": total.input_tokens,
        "cached_input_tokens": total.cached_input_tokens,
        "output_tokens": total.output_tokens,
        "cost_usd": round(cost, 4) if cost is not None else None,
        "cost_per_episode_usd": round(cost / max(ok, 1), 4) if cost is not None else None,
    }, indent=2))
    print(f"\n  summarize: {ok}/{len(dispatch)} written", flush=True)
    print(f"  tokens: input={total.input_tokens:,} (cached {total.cached_input_tokens:,})  output={total.output_tokens:,}", flush=True)
    if cost is not None:
        print(f"  cost ({cfg.model}): ${cost:.4f}  (avg ${cost/max(ok,1):.4f}/episode)", flush=True)
    print(f"  cost sidecar: {sidecar}", flush=True)
    if failed:
        print(f"  failed: {failed}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tick_file", help="path to tick_prep output JSON")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(main_async(args.tick_file, args.concurrency))


if __name__ == "__main__":
    main()
