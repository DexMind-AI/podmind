#!/usr/bin/env zsh
# preflight.sh — verify the daily.sh / whisper.sh toolchain before it next fires.
#
# Catches the class of bugs that unit tests can't see: shell glue, launchd env
# isolation, missing binaries, broken --help paths, expired secrets, wiki-tree
# drift. All checks are read-only.
#
# Two failure modes have already cost a day's digest:
#   1. `yt-dlp` symlink wiped — daily.sh's PATH was correct but the binary
#      was gone. Section 2 catches this.
#   2. Stage 7's `$EMAIL_ARG` not word-split under zsh. Section 8's daily_digest
#      --dry-run catches the same class via end-to-end exercise (a successful
#      run means argv parsed cleanly).
#
# Usage:
#   ./bin/preflight.sh           # full run — costs ~$0.01 for DS V4 dry-run
#   ./bin/preflight.sh --quick   # skip the DS V4 call (offline-safe)
#
# Exit 0 if every check passes, 1 otherwise. Suitable for a 03:30 launchd
# cron that mails the operator if anything regressed.

set -u  # NOT set -e: we want to report all failures, not abort on the first.

# Silence direnv chatter — it fires once per `uv run` invocation otherwise.
export DIRENV_LOG_FORMAT=""

PASS=0; FAIL=0
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
err()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
sect() { printf "\n── %s ──\n" "$1"; }

# Mirror exactly what com.podcast-wiki.daily.plist sets.
LAUNCHD_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

PODMIND_REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA_ROOT="${PODMIND_DATA_ROOT:-$HOME/podcast-wiki}"

# ── 1. Required env vars ─────────────────────────────────────────────
# Mirrors what daily.sh + whisper.sh + the bin/ scripts read. Anything
# unset here turns into a silent skip downstream.
sect "env vars"
required=(PODMIND_DATA_ROOT EMAIL_FROM PODCAST_DIGEST_TO)
optional=(PODMIND_EMBED_API_KEY OPENROUTER_API_KEY RESEND_API_KEY DEEPSEEK_API_KEY POCKETCASTS_TOKEN PODCAST_INDEX_KEY PODCAST_INDEX_SECRET)
for v in $required; do
  if [[ -n "${(P)v:-}" ]]; then ok "$v set"; else err "$v MISSING (required)"; fi
done
for v in $optional; do
  if [[ -n "${(P)v:-}" ]]; then ok "$v set"; else printf "  \033[33m·\033[0m %s unset (optional — feature will be skipped)\n" "$v"; fi
done

# ── 2. Binaries resolvable on the *launchd* PATH ─────────────────────
# This is the most-broken thing about cron under launchd: PATH != login shell.
# We probe with the exact PATH that the plist exports.
sect "binaries on launchd PATH"
for bin in uv yt-dlp claude python3 zsh git; do
  resolved=$(PATH="$LAUNCHD_PATH" command -v "$bin" 2>/dev/null || true)
  if [[ -n "$resolved" ]]; then
    ok "$bin → $resolved"
  else
    err "$bin not on launchd PATH"
  fi
done

# ── 3. Python imports ────────────────────────────────────────────────
# A syntax error or missing dep in any of these breaks the next cron run.
sect "podmind imports"
for mod in podmind.youtube_history podmind.sync podmind.refresh_badges podmind.transcript podmind.embeddings podmind.frontmatter podmind.paths podmind.curation podmind.slugs; do
  if (cd "$PODMIND_REPO" && PATH="$LAUNCHD_PATH" uv run python -c "import $mod" 2>/dev/null); then
    ok "$mod"
  else
    err "$mod fails to import"
  fi
done
# mlx_whisper is the heaviest optional dep. If it's missing, `try_whisper()`
# silently returns False on ImportError and the cascade stamps the episode
# `transcript_source: "none"` — burning a give-up verdict that's hard to
# distinguish later from a legitimate "no transcript anywhere" result.
# This check must run against the DATA folder's venv (where the cascade
# actually runs), not the podmind repo's venv. Caught 8525 false-"none"
# verdicts accumulated since the rename (2026-05-13).
if (cd "$DATA_ROOT" && PATH="$LAUNCHD_PATH" uv run python -c "import mlx_whisper" 2>/dev/null); then
  ok "mlx_whisper (data-folder venv — required for whisper tier)"
else
  err "mlx_whisper MISSING in data-folder venv — every cascade run will silently mark episodes as 'none'. Fix: cd $DATA_ROOT && uv sync --extra whisper"
fi

# ── 4. bin/ scripts smoke-test ───────────────────────────────────────
# `--help` for argparse scripts, no-op invocations for the few that take
# positional argv only. Each script must be runnable in the same way
# daily.sh actually invokes it — so build_stats.py runs as a script
# (its shebang requests --with matplotlib --with pandas, which `uv run python
# <path>` would bypass). Caught by this section 2026-05-13.
sect "bin scripts smoke-test"
help_scripts=(daily_digest.py ingest_run.py tick_finalize.py embed_all.py summarize.py curate.py lint.py repair_episode_links.py)
for s in $help_scripts; do
  if [[ -f "$PODMIND_REPO/bin/$s" ]]; then
    if (cd "$PODMIND_REPO" && PATH="$LAUNCHD_PATH" uv run python "bin/$s" --help >/dev/null 2>&1); then
      ok "$s --help"
    else
      err "$s --help failed"
    fi
  else
    printf "  \033[33m·\033[0m %s not present in repo\n" "$s"
  fi
done
# tick_prep.py uses positional argv (no argparse). `bin/tick_prep.py 0` is the
# cheapest exercise — emits an empty dispatch JSON and returns 0.
if (cd "$PODMIND_REPO" && PATH="$LAUNCHD_PATH" uv run python bin/tick_prep.py 0 >/dev/null 2>&1); then
  ok "tick_prep.py 0 (empty dispatch)"
else
  err "tick_prep.py 0 failed"
fi
# build_stats.py needs matplotlib + pandas which are injected by its shebang.
# Invoke as a script so the shebang fires; `-h` is harmless (script ignores it).
if PATH="$LAUNCHD_PATH" "$PODMIND_REPO/bin/build_stats.py" --help >/dev/null 2>&1 || PATH="$LAUNCHD_PATH" timeout 3 "$PODMIND_REPO/bin/build_stats.py" >/dev/null 2>&1; then
  ok "build_stats.py (shebang-launched, matplotlib loads)"
else
  err "build_stats.py shebang-launch failed (matplotlib/pandas import?)"
fi

# ── 5. Wiki directory shape ──────────────────────────────────────────
sect "wiki tree shape (data folder)"
for rel in wiki/log.md wiki/index.md wiki/episodes wiki/people wiki/topics wiki/shows wiki/digests raw/episodes raw/feeds .envrc; do
  if [[ -e "$DATA_ROOT/$rel" ]]; then ok "$rel"; else err "$rel missing"; fi
done
# Cron scripts live in the code repo (canonical) — verify both the source
# and the LaunchAgents symlinks. Bare files (non-symlink) in ~/Library/LaunchAgents
# would indicate someone forgot to re-symlink after a podmind reinstall.
sect "cron scripts (code repo + LaunchAgents symlinks)"
for rel in cron/daily.sh cron/whisper.sh cron/com.podcast-wiki.daily.plist cron/com.podcast-wiki.whisper.plist; do
  if [[ -e "$PODMIND_REPO/$rel" ]]; then ok "$rel"; else err "$rel missing in podmind repo"; fi
done
for la in com.podcast-wiki.daily.plist com.podcast-wiki.whisper.plist; do
  link="$HOME/Library/LaunchAgents/$la"
  if [[ -L "$link" ]] && [[ -e "$link" ]]; then
    ok "$la → $(readlink "$link")"
  elif [[ -f "$link" ]]; then
    err "$la is a regular file, not a symlink (re-link: ln -sf $PODMIND_REPO/cron/$la $link)"
  else
    err "$la missing from ~/Library/LaunchAgents/"
  fi
done

# ── 6. Shell scripts parse cleanly ───────────────────────────────────
sect "shell syntax"
for s in cron/daily.sh cron/whisper.sh; do
  if zsh -n "$PODMIND_REPO/$s" 2>/dev/null; then
    ok "$s"
  else
    err "$s has syntax error: $(zsh -n "$PODMIND_REPO/$s" 2>&1 | head -1)"
  fi
done

# ── 7. launchd jobs loaded ───────────────────────────────────────────
sect "launchd jobs"
for label in com.podcast-wiki.daily com.podcast-wiki.whisper; do
  if launchctl list 2>/dev/null | grep -q "$label"; then
    ok "$label loaded"
  else
    err "$label not loaded — re-run: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/${label}.plist"
  fi
done

# ── 8. daily_digest --dry-run (calls DS V4, ~$0.01) ──────────────────
# End-to-end exercise of the most-fragile stage. A pass means:
#  - argv parsed cleanly (catches zsh word-split regressions)
#  - PocketCasts /user/history auth works
#  - DeepSeek V4 call returns valid JSON
#  - the regex chain that strips backtick-wrapped wiki-links runs
# Skipped under --quick.
if [[ $QUICK -eq 0 ]]; then
  sect "daily_digest --dry-run (~\$0.01)"
  out=$(cd "$DATA_ROOT" && PATH="$LAUNCHD_PATH" uv run python "$PODMIND_REPO/bin/daily_digest.py" --hours 24 --dry-run 2>&1)
  # "No played episodes" is a HEALTHY outcome on a quiet day — the old check
  # only accepted "found N episodes" and false-failed, training the operator
  # to ignore preflight failures. Review 2026-06-12.
  if [[ $? -eq 0 ]] && echo "$out" | grep -qE "found.*episodes in window|No played episodes"; then
    ok "daily_digest end-to-end"
  else
    err "daily_digest --dry-run failed:"
    echo "$out" | sed 's/^/      /' | tail -5
  fi
else
  printf "\n── daily_digest --dry-run ──\n  \033[33m·\033[0m skipped (--quick)\n"
fi

# ── summary ──────────────────────────────────────────────────────────
printf "\n── preflight: \033[32m%d passed\033[0m, " "$PASS"
if [[ $FAIL -gt 0 ]]; then
  printf "\033[31m%d failed\033[0m ──\n" "$FAIL"
  exit 1
else
  printf "%d failed ──\n" "$FAIL"
  exit 0
fi
