#!/bin/bash
# Start logic behind the macOS app bundle "Comdirect Dashboard".
#
# The bundle only contains a stub calling this file — changes here take effect
# immediately, without reinstalling the app.
#
# Flow: if the dashboard already runs, only the browser is opened. Otherwise MongoDB is
# brought up (starting Docker if needed), uvicorn is started in the background and the
# browser is opened only once the page actually answers. A tab opened too early shows a
# connection error and has to be reloaded by hand.

set -uo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

log_start "Start"

step "Starting up …"

cd "$REPO_DIR" || fail "Project directory not found: $REPO_DIR"

# --- 1. Already running?
if reachable; then
  step "Already running — opening $URL"
  open "$URL"
  exit 0
fi

if port_taken_by_foreign_service; then
  fail "Another service answers on port $PORT. Stop it, or start the dashboard with a different port (CD_PORT)."
fi

# --- 2. Ensure Docker and the MongoDB container
ensure_docker

if docker ps --filter "name=^${MONGO_CONTAINER}$" --format '{{.Names}}' | grep -q .; then
  step "MongoDB already running."
else
  step "Starting MongoDB …"
  if docker ps -a --filter "name=^${MONGO_CONTAINER}$" --format '{{.Names}}' | grep -q .; then
    # The container exists from an earlier run: start it again instead of creating a new
    # one — its data volume holds the imported transactions.
    docker start "$MONGO_CONTAINER" >>"$LOG" 2>&1 \
      || fail "The MongoDB container \\\"$MONGO_CONTAINER\\\" could not be started. Details in the log: $LOG"
  else
    # mongod listens on 27017 inside the container; the app expects it on $MONGO_PORT.
    docker run -d --name "$MONGO_CONTAINER" -p "$MONGO_PORT:27017" "$MONGO_IMAGE" >>"$LOG" 2>&1 \
      || fail "The MongoDB container could not be created. Details in the log: $LOG"
  fi
  step "MongoDB is up."
fi

# --- 3. Start the app
step "Starting the dashboard …"

# The database settings live in .mise.toml, which only mise exports. They are set here as
# well so that a missing mise does not silently send the app to the default port 27017,
# where a different database would be waiting.
export MONGODB_URL="mongodb://localhost:$MONGO_PORT"
export DATABASE_NAME="${DATABASE_NAME:-bank_analyzer}"
export PYTHONUNBUFFERED=1

# uvicorn is started from Poetry's virtualenv directly instead of through
# "mise exec -- poetry run": mise refuses to load an untrusted .mise.toml (a fresh
# clone, or after the file changes), and inside an app bundle nobody sees that error.
# The environment .mise.toml would provide is exported above anyway.
command -v poetry >/dev/null 2>&1 \
  || fail "poetry was not found. Set up the toolchain first: mise install && mise run install"
VENV="$(poetry env info --path 2>/dev/null)"
[ -n "$VENV" ] && [ -x "$VENV/bin/uvicorn" ] \
  || fail "The Python environment is not ready. Run \\\"mise run install\\\" in $REPO_DIR."

# The server has to survive the app bundle it was started from. The bundle process exits
# as soon as this script ends, and macOS then tears down what is left in its process
# group — the server would die minutes later, without a trace in the log. os.setsid()
# puts it into a session of its own first; there is no setsid(1) on macOS.
#
# No --reload either: this is the everyday launch, not a development session. A reloader
# would spawn a second process that the stop bundle's PID file does not know about.
nohup "$VENV/bin/python" -c 'import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
  "$VENV/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
echo $! > "$PID_FILE"

# --- 4. Wait until the page really answers
step "Waiting for $URL to answer …"
for _ in $(seq 1 60); do
  reachable && break
  # A crashed start (missing dependencies, port conflict) should not be waited out.
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null \
    || fail "The dashboard stopped right after starting. Details in the log: $LOG"
  sleep 1
done

if ! reachable; then
  fail "The dashboard was started but $URL does not answer. Details in the log: $LOG"
fi

step "Ready — opening $URL"
open "$URL"
