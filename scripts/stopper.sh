#!/bin/bash
# Logic behind the macOS app bundle "Stop Personal Finance Dashboard".
#
# Stops the uvicorn process started by launcher.sh and afterwards the MongoDB container.
# The container is only stopped, NEVER removed: it holds the imported transactions and
# the categorization rules, and an accidental click in Spotlight must not delete them.
#
# The container is also left alone automatically while another client is still connected
# to it — Comdirect Dashboard I shares this mongod, with its own database inside it.
# Set CD_KEEP_MONGO=1 to keep it running unconditionally.

set -uo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

log_start "Stop"

step "Shutting down …"

stopped_something=0

# --- 1. The app process
pid="$(running_pid || true)"
if [ -n "$pid" ]; then
  step "Stopping the dashboard …"
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 15); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  # Only after a graceful shutdown had its chance: uvicorn closes the database
  # connection on SIGTERM, a SIGKILL would cut it off.
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  rm -f "$PID_FILE"
  step "Dashboard stopped (uvicorn $pid)."
  stopped_something=1
else
  rm -f "$PID_FILE"
  # Started by hand (mise run dev) instead of by the bundle? Then there is no PID file,
  # but the port tells the truth.
  if reachable; then
    listeners="$(/usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)"
    if [ -n "$listeners" ]; then
      step "Stopping the dashboard started outside the app …"
      # shellcheck disable=SC2086
      kill $listeners 2>/dev/null
      step "Stopped process(es) on port $PORT: $listeners"
      stopped_something=1
    fi
  fi
fi

# --- 2. The MongoDB container
if [ "${CD_KEEP_MONGO:-0}" = "1" ]; then
  step "Leaving MongoDB running (CD_KEEP_MONGO=1)."
elif ! docker info >/dev/null 2>&1; then
  step "Container runtime not running — nothing to stop there."
elif other_mongo_clients; then
  # Comdirect Dashboard I uses the same container with its own database. Both are meant
  # to run side by side, so quitting this one must not take the shared mongod with it —
  # the other dashboard would start failing queries without ever saying why.
  step "Leaving MongoDB running — another dashboard is still using it."
elif docker ps --filter "name=^${MONGO_CONTAINER}$" --format '{{.Names}}' | grep -q .; then
  step "Stopping MongoDB …"
  # Deliberately "stop", not "rm": the data stays in the container.
  if docker stop "$MONGO_CONTAINER" >>"$LOG" 2>&1; then
    step "MongoDB stopped."
  else
    fail "The MongoDB container \\\"$MONGO_CONTAINER\\\" could not be stopped. Details in the log: $LOG"
  fi
  stopped_something=1
else
  step "MongoDB was not running."
fi

# --- 3. The container runtime, but only if nothing else is left inside it
stop_runtime_if_idle

if [ "$stopped_something" -eq 0 ]; then
  echo "nothing was running" >>"$LOG"
  notify "The dashboard was not running."
  exit 0
fi

echo "stopped" >>"$LOG"
notify "Stopped. Your data is preserved."
