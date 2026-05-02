#!/usr/bin/env bash
# Start BirdWatcher — recorder + dashboard in parallel
set -e

if ! command -v uv &>/dev/null; then
  echo "❌  uv not found. Run './install.sh' first (or install uv)."
  exit 1
fi

# Create required directories
mkdir -p snippets
mkdir -p templates

echo ""
echo "🐦  BirdWatcher starting…"
echo "   Dashboard → http://localhost:5000"
echo "   Press Ctrl+C to stop."
echo ""

# Trap Ctrl+C and kill both child processes
cleanup() {
  echo ""
  echo "🛑  Shutting down…"
  kill "$RECORDER_PID" "$DASHBOARD_PID" 2>/dev/null
  exit 0
}
trap cleanup SIGINT SIGTERM

# Launch recorder in background
uv run python recorder.py &
RECORDER_PID=$!

# Short delay so DB is created before dashboard starts
sleep 2

# Launch dashboard
uv run python dashboard.py &
DASHBOARD_PID=$!

# Wait for either to exit
wait "$RECORDER_PID" "$DASHBOARD_PID"
