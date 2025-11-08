#!/bin/sh
set -e

LOG_DIR=/var/log
CRON_LOG="$LOG_DIR/cron.log"

mkdir -p "$LOG_DIR"
touch "$CRON_LOG"

# Ensure initial news data is available.
echo "[start.sh] Running initial news fetch..." >>"$CRON_LOG"
if ! python3 scripts/fetch_news.py >>"$CRON_LOG" 2>&1; then
  echo "[start.sh] Initial news fetch failed, continuing with existing data." >>"$CRON_LOG"
fi

# Start cron in the background.
echo "[start.sh] Starting cron daemon..." >>"$CRON_LOG"
crond -b -l 2 -L "$CRON_LOG"

exec node server.mjs

