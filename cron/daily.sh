#!/bin/zsh
# Daily podcast-wiki update — runs unattended via launchd.
#
# Stages:
#   1. yt-history   — pull fresh YouTube watch history via yt-dlp + cookies.
#   2. sync         — Pocket Casts state + cheap transcript cascade (no whisper).
#   3. refresh-badges
#   4. ingest_run   — LLM wiki ingest, capped at 100/day.
#   5. build_stats  — regenerate wiki/stats.md + charts.
#   6. embed_all    — incremental embedding cache refresh.
#   7. daily_digest — threads digest, filed + emailed.
#
# Failure policy: every stage is individually guarded so one failure doesn't
# block the rest, BUT failures accumulate in $FAILED_STAGES and trigger an
# alert email at the end. The previous design only wrote "errored" lines into
# rolling logs nobody reads — two multi-day outages (2026-05-25.., 2026-06-03..)
# were discovered only when the user asked where the digest was.

set -e

# Derive the repo root from this script's own path. The plist invokes daily.sh
# by absolute path, so dirname "$0" is always the cron/ directory.
PODMIND_REPO="$(cd "$(dirname "$0")/.." && pwd)"

# The cron scripts live in the podmind code repo but must run with the data
# folder as cwd (so `uv run` picks up the local .envrc + uv.lock there, and
# all relative paths like `wiki/log.md` resolve to the right place).
# launchd sets PODMIND_DATA_ROOT in EnvironmentVariables; fail loud if missing.
cd "${PODMIND_DATA_ROOT:?PODMIND_DATA_ROOT must be set (launchd plist EnvironmentVariables)}"

# Use local uv install; PATH may be empty under launchd.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOGFILE="wiki/log.md"
DAILY_LOG="$HOME/Library/Logs/podcast-wiki-daily.log"
TS=$(date +"%Y-%m-%d %H:%M")
TMP=$(mktemp -t podcast-wiki-daily.XXXXXX)
FAILED_STAGES=""

{
  echo "=== $TS yt-history ==="
  # Pull as deep as :ythistory will paginate (~280-500 entries before YouTube's
  # continuation cap kicks in). Policy is now broad (curation.should_exclude_yt):
  # keep everything except Shorts, music auto-channels, and known-<5min clips.
  # Materialize dedups against raw/, so re-running is free.
  uv run python -m podmind.youtube_history --limit 500 2>&1 \
    || { echo "yt-history errored (cookies may have expired; re-login to chrome.google.com to refresh)"; FAILED_STAGES="$FAILED_STAGES yt-history"; }

  echo
  echo "=== $TS sync ==="
  # --limit caps how many episodes the transcript cascade attempts per run.
  # After the PC-history-cap fix recovered ~6k listened episodes, an
  # uncapped cascade does 8-16h of yt-dlp ytsearch on audio-only shows
  # without YouTube counterparts. 200/run is a manageable per-day budget.
  uv run python -m podmind.sync --no-whisper --only-played --limit 200 2>&1 \
    || { echo "sync errored"; FAILED_STAGES="$FAILED_STAGES sync"; }

  echo
  echo "=== $TS refresh-badges ==="
  uv run python -m podmind.refresh_badges 2>&1 \
    || { echo "refresh-badges errored"; FAILED_STAGES="$FAILED_STAGES refresh-badges"; }

  echo
  echo "=== $TS pending-ingest ==="
  # Exact match via raw_dir frontmatter (same logic as tick_prep.load_ingested).
  # The old version substring-matched the episode dir name against wiki page
  # stems — false positives silently shrank the pending count and could skip
  # a night's ingest. Review 2026-06-12.
  uv run python -c "
import json
from podmind import paths
from podmind.frontmatter import read_raw_dir
ingested = set()
for p in (paths.WIKI_DIR/'episodes').glob('*.md'):
    rd = read_raw_dir(p)
    if rd:
        ingested.add(rd)
new = []
for m in paths.EPISODES_DIR.glob('*/*/meta.json'):
    try:
        meta = json.loads(m.read_text())
    except (json.JSONDecodeError, OSError):
        continue
    src = meta.get('transcript_source')
    if src and src != 'none':
        rd = f'{m.parent.parent.name}/{m.parent.name}'
        if rd not in ingested:
            new.append((m.parent.parent.name, m.parent.name, src, meta.get('title','')[:80]))
print(f'pending wiki ingest: {len(new)} episodes')
for show, ep, src, title in new[:100]:
    print(f'  [{src:13}] {show}/{ep[:50]}  — {title}')
" 2>&1 || { echo "pending wiki ingest: ? (count script errored)"; FAILED_STAGES="$FAILED_STAGES pending-count"; }
} > "$TMP" 2>&1

# Append a structured log entry to wiki/log.md
PENDING=$(grep -E "^pending wiki ingest:" "$TMP" | tail -1 || echo "pending wiki ingest: ?")
{
  echo
  echo "## [$TS] daily-cron"
  SYNC_LINE=$(grep -E "'shows':|'new':|'feed_urls_resolved'" "$TMP" | head -1 || echo "")
  TX_LINE=$(grep -E "'rss':.*'publisher':" "$TMP" | head -1 || echo "")
  REFRESH=$(grep -E "^changed=" "$TMP" | tail -1 || echo "")
  echo "- sync: ${SYNC_LINE:-(see daily-cron.log)}"
  echo "- transcripts: ${TX_LINE:-(see daily-cron.log)}"
  echo "- badges: ${REFRESH:-no change}"
  echo "- $PENDING"
  echo "- full output: ~/Library/Logs/podcast-wiki-daily.log"
} >> "$LOGFILE"

# Save full output to a rolling log
mkdir -p "$HOME/Library/Logs"
cat "$TMP" >> "$DAILY_LOG"
echo "--- end $TS ---" >> "$DAILY_LOG"
echo >> "$DAILY_LOG"

rm -f "$TMP"

# ---- Stage 4: DeepSeek V4 ingest ----
# Count comes from $PENDING (this run's own output), NOT a grep over the
# ever-growing wiki/log.md — a partially-written log entry there could
# surface a stale count from a prior day. Review 2026-06-12.
PENDING_COUNT=$(echo "$PENDING" | grep -oE '[0-9]+' | head -1 || echo 0)

if [ "${PENDING_COUNT:-0}" -gt 0 ]; then
  INGEST_LOG="$HOME/Library/Logs/podcast-wiki-ingest.log"
  # Hard cap at 100/day ≈ $1.50/day at measured DS V4 cost. (The previous
  # Claude-Opus orchestrator burned the weekly Max-20x allowance in ~4 days;
  # see CLAUDE.md "Model selection (cost discipline)".)
  INGEST_CAP=100
  N=$((PENDING_COUNT < INGEST_CAP ? PENDING_COUNT : INGEST_CAP))
  echo "=== $TS ingest_run ingest (pending=$PENDING_COUNT, processing=$N) ===" >> "$INGEST_LOG"

  "$PODMIND_REPO/bin/ingest_run.py" "$N" --concurrency 10 \
    >> "$INGEST_LOG" 2>&1 \
    || { echo "ingest_run errored" >> "$INGEST_LOG"; FAILED_STAGES="$FAILED_STAGES ingest_run"; }

  echo "--- end ingest $TS ---" >> "$INGEST_LOG"
  echo >> "$INGEST_LOG"
fi

# ---- Stage 5: regenerate stats page ----
# Invoke as a script so the shebang `#!/usr/bin/env -S uv run --with matplotlib
# --with pandas python` fires — `uv run python <path>` would bypass it and the
# import would fail. Caught by preflight 2026-05-13.
"$PODMIND_REPO/bin/build_stats.py" >> "$DAILY_LOG" 2>&1 \
  || { echo "build_stats errored" >> "$DAILY_LOG"; FAILED_STAGES="$FAILED_STAGES build_stats"; }

# ---- Stage 6: refresh embedding cache (incremental) ----
# Key resolution is handled inside embed_all.py via resolve_embed_config()
# (PODMIND_EMBED_API_KEY > secrets embed_api_key > legacy OPENROUTER_API_KEY).
# A missing key raises a clear error which lands in the guard below.
uv run python "$PODMIND_REPO/bin/embed_all.py" \
  >> "$DAILY_LOG" 2>&1 \
  || { echo "embed_all errored" >> "$DAILY_LOG"; FAILED_STAGES="$FAILED_STAGES embed_all"; }

# ---- Stage 7: daily digest ----
# zsh does NOT auto-split unquoted variables; pass --email via an array.
email_args=()
if [ -n "${PODCAST_DIGEST_TO:-}" ]; then
  email_args=(--email "$PODCAST_DIGEST_TO")
fi
uv run python "$PODMIND_REPO/bin/daily_digest.py" --hours 24 "${email_args[@]}" \
  >> "$DAILY_LOG" 2>&1 \
  || { echo "daily_digest errored" >> "$DAILY_LOG"; FAILED_STAGES="$FAILED_STAGES daily_digest"; }

# ---- Failure alert ----
# If anything failed, email a one-liner via Resend (same creds the digest
# uses). Without this, failures only land in rolling logs nobody reads.
if [ -n "$FAILED_STAGES" ]; then
  echo "[$TS] FAILED stages:$FAILED_STAGES" >> "$DAILY_LOG"
  if [ -n "${RESEND_API_KEY:-}" ] && [ -n "${EMAIL_FROM:-}" ] && [ -n "${PODCAST_DIGEST_TO:-}" ]; then
    curl -sS --max-time 30 -X POST "https://api.resend.com/emails" \
      -H "Authorization: Bearer $RESEND_API_KEY" \
      -H "Content-Type: application/json" \
      -d "{\"from\":\"${EMAIL_FROM}\",\"to\":[\"$PODCAST_DIGEST_TO\"],\"subject\":\"⚠ podcast-wiki cron: failures —$FAILED_STAGES\",\"html\":\"<p>daily.sh run at $TS had failing stages:<b>$FAILED_STAGES</b></p><p>Logs: ~/Library/Logs/podcast-wiki-daily.log and podcast-wiki-ingest.log</p>\"}" \
      >> "$DAILY_LOG" 2>&1 \
      || echo "[$TS] alert email ALSO failed" >> "$DAILY_LOG"
    echo >> "$DAILY_LOG"
  fi
fi
