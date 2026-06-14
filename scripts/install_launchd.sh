#!/bin/zsh
# Render launchd plist templates with this machine's paths, link them into
# ~/Library/LaunchAgents, and load them. Idempotent; safe to re-run.
set -euo pipefail

PODMIND_REPO="$(cd "$(dirname "$0")/.." && pwd)"
: "${PODMIND_DATA_ROOT:?Set PODMIND_DATA_ROOT to your vault path first}"

for name in daily whisper; do
  plist="com.podcast-wiki.$name.plist"
  sed -e "s|@PODMIND_REPO@|$PODMIND_REPO|g" \
      -e "s|@PODMIND_DATA_ROOT@|$PODMIND_DATA_ROOT|g" \
      -e "s|@HOME@|$HOME|g" \
      "$PODMIND_REPO/cron/$plist.template" > "$PODMIND_REPO/cron/$plist"
  ln -sf "$PODMIND_REPO/cron/$plist" "$HOME/Library/LaunchAgents/$plist"
  launchctl bootout "gui/$(id -u)/com.podcast-wiki.$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$plist"
  echo "installed + loaded $plist"
done
