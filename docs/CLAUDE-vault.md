# Podcast Wiki — Agent Instructions

You are the maintainer of an LLM-built knowledge wiki of the user's podcast subscriptions and YouTube watch history. The pattern is Karpathy's LLM-wiki applied to two streams of long-form content the user actually consumes — Pocket Casts listening history and broad YouTube watch history (everything watched, minus shorts, music, and videos under 5 minutes).

## ⚠ Changing the engine while in the air

This vault (`$PODMIND_DATA_ROOT`) depends on the `podmind` package at the code repo path (https://github.com/DexMind-AI/podmind), installed editable via `uv.sources`. Edits to the package take effect **immediately** because there's no rebuild step.

Live systems that can break the moment you save a bad file:
- **`whisper.sh`** runs under launchd (`com.podcast-wiki.whisper`, RunAtLoad + KeepAlive). It re-spawns Python every batch (~30 min). A syntax error in `podmind/transcript.py` will silently start failing all batches.
- **`daily.sh`** fires at 04:00 via launchd. Breaks if any imported module fails — and writes its own log entry in `wiki/log.md` so the failure is visible.

**Discipline before editing the podmind package**:
1. Run `uv run pytest tests/` in the repo. 100+ tests, ~0.15s; non-negotiable.
2. For changes to `transcript.py`, `youtube.py`, or anything the launchd jobs import: smoke-test with `uv run python -m podmind.<module> --help` to surface import errors immediately.
3. **Run `./bin/preflight.sh`** before any edit to `cron/daily.sh`, `cron/whisper.sh`, or anything they invoke. ~47 checks (count is dynamic), ~10s — verifies the launchd env, PATH-resolvability of every binary, podmind imports, bin/ scripts, wiki tree shape, plist symlinks, and exercises `daily_digest --dry-run`.
4. After committing, watch `~/Library/Logs/podcast-wiki-whisper.log` for one cycle (~5 min) to confirm whisper.sh still runs cleanly.

**Don't refactor `podmind/paths.py` or `bin/_lib.py` without extra care** — they're the import-time entry points; a bug there poisons everything.

**Two-repo workflow**: ALL code lives in the podmind repo (`podmind/`, `bin/`, `cron/`). The vault (`$PODMIND_DATA_ROOT`) contains only data (`raw/`, `wiki/`, `.envrc`, secrets) — no executable code. The `~/Library/LaunchAgents/com.podcast-wiki.*.plist` files are symlinks to `cron/com.podcast-wiki.*.plist` in the code repo, so edits to plists or shell scripts are versioned. `daily.sh` and `whisper.sh` read `$PODMIND_DATA_ROOT` (set by the plists) and `cd` there on entry, so relative paths like `wiki/log.md` resolve to the vault.

## Model selection (cost discipline)

**Primary path — the configured LLM provider** (`bin/ingest_run.py [N]`). DeepSeek V4 is the tested default (set via `PODMIND_LLM_*` env vars or `secrets.json`; see `.env.example`). No Claude weekly burn. Use this for routine ingest of the entire backlog.

**Cost (measured, not estimated):** ~**$0.015/episode** for fresh episodes with DeepSeek V4 — but it's high-variance because **~92% of cost is reading the transcript** (input tokens), so cost scales with transcript length, not episode count. Think of it as **~$0.0675 per 1000 KB of transcript** + ~$0.0013 flat output. Concretely:
- short clip (~25 KB): ~$0.002 · median episode (~166 KB): ~$0.012 · long interview (capped at 400 KB): ~$0.027
- the 400 KB transcript input cap (`bin/summarize.py`) bounds worst-case per-episode cost at ~$0.027
- a 100-episode `ingest_run` ≈ **$1.50**

Two cost traps to know:
- Re-runs of *recently-summarized* episodes look ~3× cheaper because the provider prompt-caches the transcript. This only happens within the provider's short cache TTL — normal backlog draining is always cache-cold. `bin/summarize.py` writes real token+cost telemetry to `/tmp/podmind-results/_cost.json` after every run; trust that over any estimate.
- The real cost lever is transcript truncation (fewer input tokens), not a cheaper model.

Run sensitivity audit (`bin/sensitivity_audit.py`) after a backlog drain to flag China-PRC sensitive episodes for optional re-summarization via Sonnet 4.6 — DeepSeek hosted API softens or omits some sensitive framings (Tiananmen, Xinjiang, Taiwan, CCP-critique).

If using Claude as the orchestrator (autonomous-loop ticks), **use a mid-tier model (e.g. Sonnet) as the parent**. The orchestration is dispatch + path-resolution + log annotation; using an expensive frontier model for dispatch burns budget fast — reserve frontier models for tasks that need judgment.

Reserve a **frontier model (e.g. Opus)** explicitly for:
- Lint passes that require schema-drift judgment.
- Synthesis writes (`query` write-path) that pull in many cross-references.
- CLAUDE.md amendments / schema co-evolution.
- One-off debugging where the cause is non-obvious from logs.

Subagents stay on Haiku (transcript <150KB) or Sonnet (≥150KB) — that split is in `bin/tick_prep.py`.

## Per-tick orchestration scripts

To keep parent context small (cache-friendly) and reduce conversation back-and-forth:

- `bin/tick_prep.py [N]` — emits next N pending listened episodes as a JSON dispatch table; transparently quarantines byte-identical yt-dlp duplicates among pending. Use the `dispatch` array directly to spawn parallel summary agents.
- `bin/tick_finalize.py <batch> [--note "..."] [--corrupt rd:reason ...] [--quarantined-dups rd:dup_of ...]` — takes JSON results from `/tmp/podmind-results/*.json`, writes episode pages, ensures people/topic stubs, updates show pages, regenerates `wiki/index.md` (preserving the Synthesis section), and appends a structured log entry.

A clean tick is now: `tick_prep` → 12 parallel agents writing to `/tmp/podmind-results/{01..12}.json` → `tick_finalize`. Three bash calls total. Drop in any per-tick corruption flagged by an agent via `--corrupt`.

### Curation scripts (one-shot, run when needed)

- `bin/yt_prefilter.py [--dry-run]` — quarantines YT episodes not worth transcribing. Drops `-topic` channels (already handled at ingest time too — see below) and any channel listed in `EXCLUDE_CHANNELS` at the top of the file. Setting `transcript_source = "none"` + `dropped_reason` so cascade and whisper both skip. Edit the set in-file to add more channels.
- `bin/sensitivity_audit.py [--threshold N]` — scans transcripts for China-PRC sensitive markers (Tiananmen, Xinjiang, Taiwan, CCP-critique, dissidents, etc.). Output is a flagged list — re-run those episodes via Sonnet/Opus if you want to verify the LLM provider didn't soften the framing. Threshold defaults to 3 hits; raise to 5 for higher precision.
- `bin/build_stats.py` — regenerates `wiki/stats.md` + 6 PNG charts in `wiki/stats/` (year volume, monthly PC-vs-YT timeline, calendar heatmap, top shows, topic evolution, coverage funnel). Idempotent. Filters out music shows (`-topic` / `vevo`) at the data-loading layer; safe to re-run after any ingest.

### Auto-ingest watchdog pattern

For long backfills, chain the transcript pull and LLM ingest unattended:

```bash
# 1. Start transcript cascade in background, capture PID
nohup uv run python -m podmind.transcript --no-whisper --limit 5000 \
  > /tmp/yt_transcribe.log 2>&1 &
TX_PID=$!

# 2. Watchdog: wait for TX_PID, then drain ingest in 500-batch loops until pending=0
nohup bash -c "
  while ps -p $TX_PID > /dev/null 2>&1; do sleep 30; done
  while true; do
    pending=\$(./bin/tick_prep.py 1 | uv run python -c 'import json,sys; print(json.load(sys.stdin)[\"pending_total\"])')
    [ \"\$pending\" -eq 0 ] && break
    ./bin/ingest_run.py 500 --concurrency 10
    sleep 5
  done
" > /tmp/auto_ingest.log 2>&1 &
```

Both processes survive parent shell exit. The Monitor tool can watch `/tmp/yt_transcribe.log` for line-count milestones if you want progress events.

### Music filtering (defense in depth)

Music is excluded at three layers:
1. **Ingest time** — `podmind.youtube._is_music_channel()` blocks `*-topic` and `*vevo*` channels in `materialize()`. New episode dirs are never created. (Applies to both `youtube_history.py` daily pull and `youtube ingest` Takeout import.)
2. **Curation** — `bin/yt_prefilter.py` quarantines any music dirs that slipped past (legacy) by setting `transcript_source = "none"`.
3. **Stats** — `bin/build_stats.py` filters music shows from analytics so they don't pollute top-N charts.

Agents return JSON dicts with these fields (see `to_episode` in `bin/tick_finalize.py` for the canonical adapter; people/topics may be either dicts or `[slug, name, role/why, note]` tuples):

```jsonc
{
  "raw_dir": "<show-slug>/<episode-dir>",
  "show_slug": "<kebab>",
  "show_name": "<from meta>",
  "date": "YYYY-MM-DD",
  "title": "...",
  "title_slug": "<kebab ≤50>",
  "guests": ["..."],
  "listened": true,
  "played_up_to": 0,
  "duration_min": 0,
  "transcript_source": "youtube",
  "hook": "≤140 chars",
  "takeaways": ["..."],
  "quotes": [{"text": "...", "attribution": "...", "timestamp": "HH:MM:SS"}],
  "people": [{"slug": "...", "name": "...", "role": "...", "note": "..."}],
  "topics": [{"slug": "...", "name": "...", "why": "..."}]
}
```

## Three-layer architecture

```
$PODMIND_DATA_ROOT/          # the vault — data only, no code
├── raw/                    # IMMUTABLE. Never edit. Source of truth.
│   ├── feeds/              # subscriptions.json (PC) + feed_urls.json (iTunes-resolved RSS URLs)
│   ├── audio/              # ephemeral; whisper downloads here, deletes after
│   └── episodes/
│       ├── <show-slug>/<yyyy-mm-dd>-<title-slug>/    # PC-sourced episode
│       │   ├── meta.json
│       │   ├── transcript.vtt   # timestamped, when available
│       │   └── transcript.md    # plain text (always present once transcribed)
│       └── yt-<channel-slug>/<watched-date>-<title-slug>/    # YouTube-watch-history-sourced
│           └── (same files; meta.json has extra youtube_* fields)
├── wiki/                   # YOU OWN THIS. Markdown, Obsidian-compatible.
│   ├── index.md            # Catalog of every wiki page (you keep updated)
│   ├── log.md              # Append-only chronicle (you append on every action)
│   ├── episodes/           # One file per ingested episode
│   ├── shows/              # One file per podcast
│   ├── people/             # Hosts, guests, recurring figures
│   ├── topics/             # Cross-cutting concepts
│   └── synthesis/          # Multi-episode analyses, on request
└── CLAUDE.md               # This file (your copy, placed in vault root).
```

The podmind code repo (https://github.com/DexMind-AI/podmind) contains:
```
podmind/                    # Pipeline package. Modify when asked; ask first for non-trivial changes.
│   ├── sync.py             # CLI entry: full sync + transcribe cascade
│   ├── pocketcasts.py      # PC API client + iTunes feed-URL resolver + RSS merge
│   ├── transcript.py       # Transcript cascade (rss → publisher → podcast-index → youtube → whisper)
│   ├── youtube.py          # CLI entry: ingest Google Takeout watch-history
│   ├── paths.py            # Canonical paths and slug helpers
│   └── secrets.py          # Token/key store (~/.config/podmind/secrets.json)
bin/                        # Operational scripts
cron/                       # launchd shell wrappers (daily.sh, whisper.sh, plists)
```

## meta.json schema

```jsonc
{
  // Common to all sources
  "show": "Signals & Noise",
  "show_uuid": "...",            // PC's UUID (null for YouTube)
  "show_feed_url": "https://...", // RSS feed URL (resolved via iTunes for PC; null for YouTube)
  "title": "Episode title here",
  "guid": "<rss-guid or yt:VIDEOID>",
  "pub_date": "2026-04-24",
  "published_iso": "2026-04-24T04:00:00+00:00",
  "duration_sec": 3000,
  "audio_url": "https://...",     // .mp3 enclosure for PC; YouTube URL for yt-*
  "listened": true,
  "played_up_to": 3150,
  "playing_status": 3,            // 1 unplayed, 2 in-progress, 3 played
  "transcript_source": null,      // null | "rss" | "publisher" | "podcast-index" | "youtube" | "whisper" | "none"

  // YouTube-only (also: "source": "youtube", "watched_at": "<iso>")
  "youtube_url": "https://www.youtube.com/watch?v=...",
  "youtube_video_id": "..."
}
```

## Transcript cascade

In order, each tier writes `transcript.vtt` (when timestamps available) + `transcript.md`, sets `transcript_source`, and stops:

1. **`rss`** — Podcasting 2.0 `<podcast:transcript>` tag in the show's RSS feed.
2. **`publisher`** — known-publisher web scraper. Often blocked by paywalls/bot-detection.
3. **`podcast-index`** — looks the episode up in api.podcastindex.org and pulls any registered transcript URL. Requires `podcast_index_key` + `podcast_index_secret` in secrets.
4. **`youtube`** — `yt-dlp` with `--write-sub --write-auto-sub`. For YouTube-sourced episodes uses `meta.youtube_url` directly; for PC episodes does a `ytsearch1:<show> <title>` search.
5. **`whisper`** — local `mlx-whisper` (M-series Mac). Last resort. Requires `uv sync --extra whisper`.

If all tiers fail, `transcript_source` is set to `"none"`. Re-running the cascade skips episodes already at any non-null source — to retry, set the field back to `null`.

## Listened-state badges (mandatory on every episode citation)

Every wiki link to an episode MUST carry a state badge derived from `meta.json`:

- `🎧` — `listened: true` (played to end OR `played_up_to` ≥ 95% of duration)
- `▶ NN%` — `listened: false` AND `played_up_to > 0`
- `⚪` — `listened: false` AND `played_up_to == 0`

Example:

```markdown
- [[episodes/2026-04-20-signals-and-noise-on-ai-agents]] 🎧 — AI agents deep dive
- [[episodes/2026-04-25-signals-and-noise-hardware-cycles]] ⚪ — hardware cycles (not yet heard)
- [[episodes/2026-04-26-signals-and-noise-interview-roundup]] ▶ 42%
```

This makes "what have I actually heard about X?" answerable by filtering for 🎧 in greps.

## Operations

### `ingest <episode-slug | --since <date> | --new>`

When the user asks you to ingest one or more episodes:

1. Read `wiki/log.md` to find the last ingest checkpoint.
2. For each new episode dir under `raw/episodes/` (both `<show>/...` and `yt-<channel>/...` patterns):
   - Read `meta.json` and `transcript.md`.
   - If `transcript_source: none`, skip (note in log).
   - Write `wiki/episodes/<show-slug>-<date>-<title-slug>.md` with:
     - YAML frontmatter (show, date, guests, listened, played_up_to, duration_min, transcript_source).
     - Listened badge in body.
     - 3–8 bullet "key takeaways."
     - Notable quotes (with `t=HH:MM:SS` links into `transcript.vtt` if it exists).
     - Cross-links to `[[shows/...]]`, `[[people/...]]`, `[[topics/...]]`.
   - For each person mentioned: ensure `wiki/people/<name-slug>.md` exists; append a one-line citation back to the episode.
   - For each topic touched: same, in `wiki/topics/`.
   - Update `wiki/shows/<show-slug>.md` episode list.
3. Update `wiki/index.md` with the new pages.
4. Append a structured entry to `wiki/log.md`:
   ```
   ## [YYYY-MM-DD HH:MM] ingest
   - episode: <slug>
   - touched: episodes/foo.md, people/bar.md, topics/baz.md
   - listened: true|false
   ```

A single episode ingest typically touches 5–15 wiki pages. That's expected.

For very large batches (dozens of episodes), dispatch parallel summarization sub-agents and write files yourself to avoid race conditions on shared pages (people, topics, index, log).

### `query <question>`

Queries are a **write-path, not a read-path.** Per Karpathy's original LLM-wiki pattern (https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): *"good answers can be filed back into the wiki as new pages... your explorations compound in the knowledge base just like ingested sources do."*

Every non-trivial query MUST leave the wiki better than it found it. Do not "offer" — file the artefact, then tell the user.

1. **Skim `wiki/index.md`** for relevant pages.
2. **Read those pages**; follow `[[wiki-links]]` into related entities/topics.
3. **Fall back to raw transcripts** (`raw/episodes/*/transcript*.{md,vtt}`) only if the wiki layer doesn't have the answer. Remember: only ~1% of raw episodes have a transcript at all (`transcript_source: null` ⇒ invisible to grep). State this caveat when an answer might be incomplete.
4. **Search methodology** — when looking for tools, products, people, or any open category:
   - Do NOT start from a curated keyword list. Keyword lists embed your priors and miss adjacent categories (e.g. searching only generative-AI products misses programmatic frameworks like Remotion).
   - Start with open-ended patterns over the verbs the user cares about (`\b(use|using|recommend|tried|switched to|generate|edit|render).{0,40}<noun>\b`) and let product names emerge.
   - Use word boundaries (`\b...\b`) and disambiguate false positives in context (e.g. "Kling" the AI vs "Klingbeil" the politician; "Runway" the tool vs "airport runway").
   - Mention count ≠ endorsement. A single dedicated CEO interview outweighs ten name-drops in roundups.
5. **Synthesize the answer** with citations of the form `[[episodes/<slug>]]` plus the listened badge (🎧 / ⚪ / ▶ NN%) so the user knows whether the source is heard material. Pick the right output format for the question — a markdown page is the default, but a comparison table, a chart (matplotlib), a Marp slide deck, or a canvas can be more useful and can also be filed back into `wiki/synthesis/` as the artefact.
6. **Persist the synthesis** if the answer was non-trivial (more than a single-page lookup): write `wiki/synthesis/<slug>.md` with frontmatter `synthesized: <date>`, the question verbatim, sources searched, methodology notes (especially anything that went wrong on the first pass — those are the most valuable notes for future-you), and the answer body. Link the new page from `wiki/index.md` under the `## Synthesis` section (create the section if missing).
7. **Surface gaps** in the synthesis page AND in the log entry:
   - Un-ingested episodes that turned out to be high-value sources for this topic — name them as ingest candidates.
   - Broken or missing cross-links discovered while answering.
   - Near-duplicate pages in `topics/` or `people/` you stumbled across (lint candidates).
   - Stale state observations (e.g. orphaned files in `raw/audio/`, mismatched badges).
8. **Append a structured entry to `wiki/log.md`**:
   ```
   ## [YYYY-MM-DD HH:MM] query
   - question: "<verbatim question>"
   - searched: <wiki layer | raw transcripts (~N files) | both>
   - followups: "<short summaries of follow-up questions, if any>"
   - synthesis filed: <path or "none — single-page answer">
   - touched: <comma-separated paths>
   - gaps surfaced: <bulleted list>
   ```

Trivial queries (single-page lookup, factual yes/no, count of files) do not need a synthesis page — but DO log them under `## [date] query` with `synthesis filed: none`. The log is how the wiki learns which questions get asked, which is itself a signal for what to ingest next.

### `lint`

Periodic health pass:
- Orphan pages (not referenced from `index.md`).
- Stale listened badges (compare wiki vs. current `meta.json`).
- Broken cross-links — pages referenced from episodes that don't exist as files yet (common after large batch ingests).
- Person/topic pages with only one citation (candidates for merge or removal).
- Near-duplicate pages — multiple slug variants of the same concept (e.g. `video-generation-ai` / `ai-video-generation` / `video-synthesis-ai`). Propose a merge target.
- Concepts mentioned across episodes but lacking their own page (e.g. a recurring tool or framework that has no `topics/<slug>.md`).
- `transcript_source: none` episodes — list as gaps.
- Title/transcript mismatches (e.g., yt-dlp's `ytsearch1` matched a Conrad Roy true-crime video for a "Uma Roy" Network State episode — surname collision with a different person). Skip with a `lint`-flag note rather than ingesting the wrong content.
- Contradictions across pages (best-effort).
- **Stale claims** — older pages whose factual content has been superseded by newer episodes (e.g. a 2024 page on a company's strategy that a 2026 episode contradicts). Note both, propose a reconciliation.
- **Data gaps fillable by web search** — questions raised in synthesis pages where one targeted web fetch (e.g. an official product page, a key Wikipedia article, the transcript of a guest's other appearance) would close the loop. List as web-fetch candidates rather than executing them silently.
- **Open questions and missing sources** — proactively suggest new questions worth investigating and new shows/episodes worth ingesting, based on patterns visible in the wiki (e.g. a topic page with citations only from one show, a person page with only an ⚪/▶ trail).

Append findings to `wiki/log.md` under `## [date] lint` and propose fixes.

### `refresh-badges`

Walk every `wiki/episodes/*.md`, re-read the corresponding `meta.json`, update the badge in the frontmatter and the first body line. No other changes. This is what handles "I just listened to that episode" without re-deriving the wiki.

### Pipeline commands the user typically runs (you may run when asked)

- `uv run python -m podmind.sync [--no-whisper] [--no-transcribe] [--only-played] [--limit N]`
- `uv run python -m podmind.transcript [--only-played] [--no-whisper] [--whisper-only] [--episode <dir>]`
- `uv run python -m podmind.youtube ingest <watch-history.json|.html> [--include-all] [--limit N]`
- `uv run python -m podmind.pocketcasts login` (one-time, stores token in secrets)

### Background whisper job (decoupled from the daily-cron)

`whisper.sh` is a long-lived loop launched by `com.podcast-wiki.whisper` (RunAtLoad + KeepAlive). AC-power gating happens **inside Python** via `transcript.py --require-ac`: `pmset -g ps` is checked before every single episode in `transcribe_all`, and the batch aborts the moment the Mac unplugs (mid-batch — worst-case battery drain is one in-flight episode). The shell wrapper handles only the outer "fully on battery, idle" and "backlog empty, idle" sleeps, so we don't pay Python startup cost when there's nothing to do. Default batch size is `WHISPER_BATCH=50` (Python aborts mid-batch on AC drop, so a large batch is safe). Single-instance via `/tmp/podcast-wiki-whisper.lock`. Logs to `~/Library/Logs/podcast-wiki-whisper.log`.

The daily.sh cascade runs `--no-whisper` and leaves `transcript_source=null` for episodes where the cheap tiers (rss/publisher/podcast-index/youtube) failed; the whisper job picks those up while the Mac is plugged in. Newly-whispered episodes get ingested by the next daily.sh ingest pass — no special handling needed in the wiki layer.

`transcribe_all` sorts candidates by `pub_date` descending (fall back to `watched_at` then to filesystem mtime), so freshly-listened episodes are transcribed first regardless of the alphabetical position of their show directory. This keeps the daily ingest aligned with what you actually listened to most recently.

### Log entry types

- `## [date] sync` — written by `sync.py` (Pocket Casts pull + transcript cascade summary)
- `## [date] youtube-ingest` — written by `youtube.py ingest`
- `## [date] ingest` — you write after a wiki ingest
- `## [date] query` — you write after every user query (trivial or not); records the question, scope of search, gaps surfaced, and any synthesis page filed
- `## [date] lint` — you write after a lint pass
- `## [date] refresh-badges` — you write after a badge refresh
- `## [date] schema` — you write when CLAUDE.md, scripts in `bin/`, or pipeline modules in `podmind/` change in a way future sessions need to know about
- `## [date] cleanup` — you write after physical disk operations (deleting dirs, moving files outside the normal flow)
- `## [date] yt-prefilter` — written by `bin/yt_prefilter.py` (or you, after running it manually)
- `## [date] stats` — optional; write when `wiki/stats.md` is regenerated and noteworthy headline numbers shifted

## Conventions

- **Filenames**: kebab-case, ASCII-folded. `podmind/paths.py` is canonical (`show_slug`, `episode_slug`, `episode_dir`).
- **Frontmatter**: YAML, between `---` lines. Required keys: `show, date, listened, played_up_to, duration_min, guests, transcript_source, raw_dir`. The `raw_dir:` value is `<show-slug>/<episode-slug>` (e.g. `yt-signals-and-noise/2026-02-01-ai-deep-dive`) — this is what `refresh_badges` uses to find the right `meta.json` when both a PC and YouTube variant exist.
- **Cross-links**: Obsidian `[[wiki-link]]` style. Relative to `wiki/`. Broken links are acceptable temporarily — Obsidian creates the target on click.
- **Quotes**: prefer pull-quotes with `> ` block, attribution + timestamp.
- **No silent edits to `raw/`** — if a transcript is wrong, note it in the wiki entry, don't rewrite the source.
- **YouTube watch-history dedup**: when a YouTube episode and a PC episode of the same show clearly correspond to the same content, prefer one wiki page. Flag the duplicate in log.
- **Channel/title surname collisions** in YouTube ingest happen (e.g., yt-dlp's `ytsearch1` matched a Conrad Roy true-crime video for a "Uma Roy" Network State episode). Skip with a `lint`-flag note rather than ingesting the wrong content.

## Schema co-evolution

This file is the **schema** layer in the three-layer pattern. It's not frozen — it co-evolves with the wiki as you and the user discover what works.

When during any operation you discover that an instruction is missing, ambiguous, or has drifted from the canonical pattern (Karpathy's gist at https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), update CLAUDE.md *in the same session*, log the change under `## [date] query` (or `lint`, whichever fits), and explain the diff to the user. Do not wait to be asked. The schema is the highest-leverage thing in the repo: a one-sentence fix here propagates to every future session.

Examples of session-discovered schema drift worth fixing immediately:
- A protocol step the agent has been silently skipping (e.g., "offer to file synthesis" → never filed).
- A category of artefact the schema doesn't name (e.g., comparison tables, charts, slide decks).
- A `lint` check the schema doesn't list (e.g., near-duplicate topic pages).
- A pattern the user keeps re-explaining ("treat queries as write-paths" — should be in the file, not the chat).

## What you do NOT do

- Delete from `raw/`.
- Rebuild the entire wiki from scratch — always incremental, log-anchored.
- Modify `podmind/*.py` for non-trivial pipeline changes without asking. Bug fixes and small extensions on request are fine.
- Run sync / transcribe / youtube-ingest commands without explicit user request — they hit external APIs and take significant wall time. Confirm scope first when asked.
