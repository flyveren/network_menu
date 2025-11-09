#!/bin/sh
set -e

LOG_DIR=/var/log
CRON_LOG="$LOG_DIR/cron.log"

mkdir -p "$LOG_DIR"
touch "$CRON_LOG"

# Ensure initial data is available.
echo "[start.sh] Running initial weather fetch..." >>"$CRON_LOG"
if ! python3 scripts/fetch_weather.py >>"$CRON_LOG" 2>&1; then
  echo "[start.sh] Initial weather fetch failed, continuing with existing data." >>"$CRON_LOG"
fi

for category in all indland udland kultur debat; do
  echo "[start.sh] Running initial news fetch for category '${category}'..." >>"$CRON_LOG"
  if ! python3 scripts/fetch_news.py --category "$category" >>"$CRON_LOG" 2>&1; then
    echo "[start.sh] Initial news fetch failed for category '${category}', continuing with existing data." >>"$CRON_LOG"
  fi
done

# Start cron in the background.
echo "[start.sh] Starting cron daemon..." >>"$CRON_LOG"
crond -b -l 2 -L "$CRON_LOG"

exec node server.mjs

