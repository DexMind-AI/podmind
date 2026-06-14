#!/bin/zsh
# Continuous whisper runner — decoupled from daily.sh, AC-power gated.
#
# Long-lived loop. AC-power gating happens INSIDE Python (--require-ac):
# transcript.py checks pmset before every episode and aborts the batch
# the moment the Mac unplugs. Worst-case battery drain is therefore one
# in-flight episode (model already loaded, currently transcribing).
#
# This script's outer loop only handles "fully on battery" idling and
# "backlog empty" idling — keeping us out of an expensive Python startup
# when there's nothing to do. Single-instance via lockfile. Started by
# launchd at boot/login (RunAtLoad) and auto-restarted on crash (KeepAlive).
#
# New transcripts are silently consumed by the next daily.sh ingest pass.

set -e

# The cron scripts live in the podmind code repo but must run with the data
# folder as cwd (so `uv run` picks up the local .envrc + uv.lock there).
# launchd sets PODMIND_DATA_ROOT in EnvironmentVariables; fail loud if missing.
cd "${PODMIND_DATA_ROOT:?PODMIND_DATA_ROOT must be set (launchd plist EnvironmentVariables)}"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

BATCH="${WHISPER_BATCH:-50}"                   # episodes per Python invocation; Python aborts mid-batch on AC drop
BATTERY_SLEEP="${WHISPER_BATTERY_SLEEP:-300}"
IDLE_SLEEP="${WHISPER_IDLE_SLEEP:-1800}"
USER_ACTIVE_SLEEP="${WHISPER_USER_ACTIVE_SLEEP:-120}"  # 2min — short so we resume soon after user steps away
USER_IDLE_THRESHOLD="${WHISPER_USER_IDLE_SEC:-300}"    # require ≥5min of no input before starting a batch
LOCKFILE="/tmp/podcast-wiki-whisper.lock"
LOGFILE="$HOME/Library/Logs/podcast-wiki-whisper.log"

mkdir -p "$HOME/Library/Logs"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"; }

if [ -f "$LOCKFILE" ] && kill -0 "$(cat "$LOCKFILE" 2>/dev/null)" 2>/dev/null; then
  log "another whisper already running (pid=$(cat "$LOCKFILE")); exit"
  exit 0
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"; log "exit"' EXIT

on_power() {
  pmset -g ps 2>/dev/null | head -1 | grep -q "AC Power"
}

log "=== continuous whisper start (batch=$BATCH, ac+user-idle gating in-Python) ==="

# Quick user-active probe: HIDIdleTime in nanoseconds; small = recent input.
# Implemented in shell so we can skip the Python startup entirely while the
# user is typing (saves ~1s of process spawn + import per check).
user_idle_seconds() {
  local nanos
  nanos=$(ioreg -c IOHIDSystem 2>/dev/null \
    | awk '/HIDIdleTime/ {gsub(/.*= /,""); print}' \
    | sort -n | head -1)
  if [ -z "$nanos" ]; then
    echo "999999"  # fail open
  else
    echo "$((nanos / 1000000000))"
  fi
}

while true; do
  if ! on_power; then
    log "on battery, sleep ${BATTERY_SLEEP}s"
    sleep "$BATTERY_SLEEP"
    continue
  fi

  idle=$(user_idle_seconds)
  if [ "$idle" -lt "$USER_IDLE_THRESHOLD" ]; then
    log "user active (${idle}s idle, need ≥${USER_IDLE_THRESHOLD}s), sleep ${USER_ACTIVE_SLEEP}s"
    sleep "$USER_ACTIVE_SLEEP"
    continue
  fi

  # Run the FULL cascade (cheap tiers first, whisper as fallback). Cheap tiers
  # are nearly free vs whisper's ~4 min GPU/episode. Python also re-checks
  # AC + user-idle between every episode, so a batch that starts cleanly can
  # still abort mid-way if the user comes back to their desk.
  output=$(uv run python -m podmind.transcript --only-played \
    --require-ac \
    --require-user-idle-sec "$USER_IDLE_THRESHOLD" \
    --limit "$BATCH" 2>&1) || true
  echo "$output" | tail -10 >> "$LOGFILE"

  # Python prints machine-readable sentinels (plain print, single line).
  # The old approach grepped rich's dict repr on one line — but rich
  # pretty-prints multi-line in a non-tty, so the pattern NEVER matched,
  # the idle sleep never fired, and the loop re-spawned Python (a full
  # vault metadata scan) continuously: the "fork failed" resource
  # exhaustion incidents. Review 2026-06-12.
  if echo "$output" | grep -q "ABORTED_ERROR=1"; then
    # Persistent failure (e.g. mlx_whisper missing). Back off hard —
    # KeepAlive would otherwise have us re-crashing every cycle.
    log "batch aborted on error (see above), sleep ${IDLE_SLEEP}s"
    sleep "$IDLE_SLEEP"
  elif echo "$output" | grep -q "BACKLOG_EMPTY=1"; then
    log "backlog empty, sleep ${IDLE_SLEEP}s"
    sleep "$IDLE_SLEEP"
  fi
done
