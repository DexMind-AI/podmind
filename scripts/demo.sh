#!/usr/bin/env bash
# Zero-key demo: run the real tick pipeline over the bundled demo vault.
# No accounts, no API keys — summarizer results are pre-baked.
#
# Pipeline: bin/tick_prep.py (find pending episodes) → pre-baked summaries
# copied into /tmp/podmind-results → bin/tick_finalize.py (write wiki pages,
# people/topic stubs, show page, index, log).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"  # uv run resolves the project from cwd
export PODMIND_DATA_ROOT="$REPO/examples/demo-vault"

mkdir -p "$PODMIND_DATA_ROOT"/wiki/{episodes,people,topics,shows,synthesis}
# The wiki ships pre-generated so the repo shows output without running anything.
# Delete the episode pages so tick_prep has real work to do; finalize will
# regenerate them identically (only wiki/log.md gains an entry per run).
rm -f "$PODMIND_DATA_ROOT"/wiki/episodes/*.md

echo "== tick_prep: scanning demo vault for pending episodes =="
uv run python "$REPO/bin/tick_prep.py" 3 | tee /tmp/podmind-demo-tick.json

# NOTE: /tmp/podmind-results is shared with real ingest runs — a concurrent
# real ingest would conflict with the demo (we clear the dir here).
rm -rf /tmp/podmind-results && mkdir -p /tmp/podmind-results
cp "$PODMIND_DATA_ROOT"/prebaked-results/*.json /tmp/podmind-results/

echo
echo "== tick_finalize: writing wiki pages from pre-baked summaries =="
uv run python "$REPO/bin/tick_finalize.py" 1 --note "demo ingest (pre-baked summaries)"

echo
n_eps=$(ls "$PODMIND_DATA_ROOT"/wiki/episodes/*.md 2>/dev/null | wc -l | tr -d ' ')
echo "$n_eps episodes ingested → $PODMIND_DATA_ROOT/wiki/"
echo "Done. Open $PODMIND_DATA_ROOT/wiki/ in Obsidian (or any editor)."
