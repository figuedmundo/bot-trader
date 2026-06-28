#!/bin/bash
set -Eeuo pipefail
IFS=$'\n\t'

PORT="${1:-9222}"
MODE="${2:-}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "Error: port must be numeric (got: $PORT)" >&2
  exit 2
fi

CDP_URL="http://127.0.0.1:$PORT/json/version"

if curl -fsS --max-time 2 "$CDP_URL" > /dev/null 2>&1; then
  echo "CDP already ready at $CDP_URL"
  curl -fsS "$CDP_URL" | python3 -m json.tool 2>/dev/null || curl -fsS "$CDP_URL"
  exit 0
fi

# Auto-detect TradingView install location
APP=""
LOCATIONS=(
  "/Applications/TradingView.app/Contents/MacOS/TradingView"
  "$HOME/Applications/TradingView.app/Contents/MacOS/TradingView"
)

for loc in "${LOCATIONS[@]}"; do
  if [ -f "$loc" ]; then
    APP="$loc"
    break
  fi
done

# Fallback: search with mdfind (Spotlight)
if [ -z "$APP" ]; then
  APP=$(mdfind "kMDItemCFBundleIdentifier == 'com.niceincontact.TradingView'" 2>/dev/null | head -1 || true)
  if [ -n "$APP" ]; then
    APP="$APP/Contents/MacOS/TradingView"
  fi
fi

# Fallback: find any TradingView.app
if [ -z "$APP" ] || [ ! -f "$APP" ]; then
  APP=$(find /Applications "$HOME/Applications" -name "TradingView.app" -maxdepth 2 2>/dev/null | head -1 || true)
  if [ -n "$APP" ]; then
    APP="$APP/Contents/MacOS/TradingView"
  fi
fi

if [ -z "$APP" ] || [ ! -f "$APP" ]; then
  echo "Error: TradingView not found."
  echo "Checked: /Applications/TradingView.app, ~/Applications/TradingView.app"
  echo ""
  echo "If installed elsewhere, run manually:"
  echo "  /path/to/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=$PORT"
  exit 1
fi

if pgrep -f "TradingView" > /dev/null 2>&1; then
  if [ "$MODE" != "--relaunch" ]; then
    echo "Error: TradingView appears to already be running, but CDP is not responding on port $PORT." >&2
    echo "Refusing to kill/relaunch automatically." >&2
    echo "If you want a forced restart, run: $0 $PORT --relaunch" >&2
    exit 3
  fi
  pkill -f "TradingView" 2>/dev/null || true
  sleep 1
fi

echo "Found TradingView at: $APP"
echo "Launching with --remote-debugging-port=$PORT ..."
"$APP" --remote-debugging-port=$PORT &
TV_PID=$!
echo "PID: $TV_PID"

# Wait for CDP to be ready
echo "Waiting for CDP..."
for i in $(seq 1 15); do
  if curl -fsS --max-time 2 "$CDP_URL" > /dev/null 2>&1; then
    echo "CDP ready at $CDP_URL"
    curl -fsS "$CDP_URL" | python3 -m json.tool 2>/dev/null || curl -fsS "$CDP_URL"
    exit 0
  fi
  sleep 1
done

echo "Warning: CDP not responding after 15s. TradingView may still be loading."
echo "Check manually: curl $CDP_URL"
exit 4
