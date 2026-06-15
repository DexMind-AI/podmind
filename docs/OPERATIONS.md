# Operations

Operator-facing detail for running podmind at scale. New here? Start with the
[README](../README.md).

## LLM provider configuration

podmind speaks two wire protocols — OpenAI-compatible `chat/completions` and the
Anthropic Messages API — so any mainstream provider works. Configure via env vars
(`PODMIND_LLM_PROVIDER` / `PODMIND_LLM_BASE_URL` / `PODMIND_LLM_MODEL` /
`PODMIND_LLM_API_KEY`) or the equivalent `llm_*` keys in `secrets.json`:

| Provider | `PODMIND_LLM_PROVIDER` | `PODMIND_LLM_BASE_URL` | `PODMIND_LLM_MODEL` (example) |
|---|---|---|---|
| **DeepSeek** (default — tested, cost-measured) | `openai-compat` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `openai-compat` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `openai-compat` | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat` |
| Ollama (local) | `openai-compat` | `http://localhost:11434/v1` | `llama3.1` |
| Anthropic | `anthropic` | `https://api.anthropic.com` | `claude-sonnet-4-6` |

Embeddings follow the same pattern with `PODMIND_EMBED_BASE_URL` /
`PODMIND_EMBED_MODEL` / `PODMIND_EMBED_API_KEY` (default: OpenRouter,
`openai/text-embedding-3-small`).

## Cost discipline

All numbers are **measured**, not estimated (telemetry written to
`/tmp/podmind-results/_cost.json` after every run):

- **~$0.015/episode** fresh on DeepSeek V4 — ~92% is transcript input tokens, so
  cost scales with transcript length (rule of thumb **~$0.0675 / 1000 KB**).
- The 400 KB transcript input cap bounds worst-case per-episode cost at **~$0.027**.
- A 100-episode backlog run lands around **$1.50**.
- Whisper transcription is free locally on Apple Silicon (~5–7 min / 60-min episode).
- Transcripts are stored xz-compressed (`transcript.vtt.xz`, ~6–7× smaller);
  `transcript.md` stays plain. `podmind compress-transcripts` migrates a vault.

## The `podmind` commands, and the scripts under them

Every verb routes to a script you can also run directly:

| `podmind` verb | Underlying | What it does |
|---|---|---|
| `ingest [N]` | `bin/ingest_run.py` | prep → LLM summarize → finalize, one command |
| `query <q>` | `bin/query.py` | semantic query, filed back as a synthesis page |
| `lint` | `bin/lint.py` | wiki health pass |
| `sync` | `python -m podmind.sync` | Pocket Casts pull + transcript cascade |
| `transcript` | `python -m podmind.transcript` | transcript cascade only |
| `digest` | `bin/daily_digest.py` | recent-listening digest |
| `stats` | `bin/build_stats.py` | regenerate stats + charts |
| `embed` | `bin/embed_all.py` | build/refresh embedding cache |
| `compress-transcripts` | `bin/compress_transcripts.py` | VTT xz migration |
| `refresh-badges` | `python -m podmind.refresh_badges` | re-derive listened badges |
| `demo` | `scripts/demo.sh` | zero-key demo |

Other internal maintenance scripts (frontmatter repair, topic-merge proposals,
cross-link enrichment, sensitivity auditing) live in the maintainer's repo and are
not part of the published toolchain.

## Automation (macOS launchd)

```bash
PODMIND_DATA_ROOT=/Users/you/my-podmind-vault ./scripts/install_launchd.sh
```

Renders and loads two jobs from the tracked plist templates in `cron/`: a **daily
04:00 pipeline** (Pocket Casts pull → YouTube history pull → transcript cascade →
LLM ingest → stats → digest) and a long-lived **whisper loop** that drains the
transcription backlog only on AC power. `bin/preflight.sh` verifies the toolchain
before it next fires.

## Transcript cascade

Five tiers, cheapest first, stop at the first hit: **rss** (Podcasting 2.0 tag) →
**publisher** (known scrapers) → **podcast-index** (registered transcript URLs) →
**youtube** (yt-dlp subs) → **whisper** (local mlx-whisper). Each writes
`transcript.vtt.xz` + `transcript.md` and sets `transcript_source`.

## Searching transcripts

`transcript.md` is plain text — grep it directly. The timestamped VTT is
xz-compressed: `xzcat <episode-dir>/transcript.vtt.xz | grep …`.
