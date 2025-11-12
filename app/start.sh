#!/bin/sh
set -e

LOG_DIR=/var/log
CRON_LOG="$LOG_DIR/cron.log"

mkdir -p "$LOG_DIR"
touch "$CRON_LOG"

# Optional self-signed certificate generation
if [ "${ENABLE_SELF_SIGNED_CERT:-}" = "true" ]; then
  DEFAULT_CERT_DIR=${SSL_CERT_DIR:-"./certs"}
  DEFAULT_KEY_PATH=${SSL_KEY_PATH:-"$DEFAULT_CERT_DIR/selfsigned-key.pem"}
  DEFAULT_CERT_PATH=${SSL_CERT_PATH:-"$DEFAULT_CERT_DIR/selfsigned-cert.pem"}

  mkdir -p "$(dirname "$DEFAULT_KEY_PATH")"

  if [ ! -f "$DEFAULT_KEY_PATH" ] || [ ! -f "$DEFAULT_CERT_PATH" ]; then
    echo "[start.sh] Generating self-signed certificate..." >>"$CRON_LOG"
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout "$DEFAULT_KEY_PATH" \
      -out "$DEFAULT_CERT_PATH" \
      -days "${SELF_SIGNED_CERT_DAYS:-365}" \
      -subj "${SELF_SIGNED_CERT_SUBJECT:-/CN=localhost}" >>"$CRON_LOG" 2>&1 || {
        echo "[start.sh] Failed to generate self-signed certificate." >>"$CRON_LOG"
      }
  else
    echo "[start.sh] Using existing self-signed certificate." >>"$CRON_LOG"
  fi

  export SSL_KEY_PATH="$DEFAULT_KEY_PATH"
  export SSL_CERT_PATH="$DEFAULT_CERT_PATH"
fi

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

# Fetch initial Voxmeter polling data
echo "[start.sh] Running initial Voxmeter poll fetch..." >>"$CRON_LOG"
if ! python3 scripts/fetch_voxmeter.py >>"$CRON_LOG" 2>&1; then
  echo "[start.sh] Initial Voxmeter fetch failed, continuing with existing data." >>"$CRON_LOG"
fi

# Start cron in the background.
echo "[start.sh] Starting cron daemon..." >>"$CRON_LOG"
crond -b -l 2 -L "$CRON_LOG"

exec node server.mjs

