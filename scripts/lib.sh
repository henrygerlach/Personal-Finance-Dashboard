#!/bin/bash
# Shared basis of the two macOS app bundles (start and stop).
# Sourced by launcher.sh and stopper.sh, never called directly.

REPO_DIR="${CD_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="$HOME/Library/Logs/Comdirect-Dashboard.log"
PID_FILE="$HOME/Library/Application Support/Comdirect-Dashboard/uvicorn.pid"

# An app launched from Spotlight does not inherit the login shell's PATH, so the tools
# used below (docker, mise, poetry) have to be found explicitly.
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$HOME/.local/share/mise/shims:$HOME/.docker/bin:$PATH"

# mise refuses to read an untrusted .mise.toml, and the poetry shim inherits that
# refusal — which inside an app bundle would look like "poetry is broken". The project
# directory this script lives in is trusted explicitly, for this process only: nothing
# is written to the global trust store, so `mise trust` still guards the shell.
export MISE_TRUSTED_CONFIG_PATHS="${MISE_TRUSTED_CONFIG_PATHS:+$MISE_TRUSTED_CONFIG_PATHS:}$REPO_DIR"

HOST="${CD_HOST:-127.0.0.1}"
PORT="${CD_PORT:-8000}"
URL="http://$HOST:$PORT"

# Mongo runs in a container; the name and the port mapping have to match .mise.toml,
# where MONGODB_URL points at localhost:27018. mongod itself listens on 27017 inside
# the container, hence the asymmetric mapping.
MONGO_CONTAINER="${CD_MONGO_CONTAINER:-mongo-bank}"
MONGO_PORT="${CD_MONGO_PORT:-27018}"
MONGO_IMAGE="mongo:latest"

mkdir -p "$(dirname "$LOG")" "$(dirname "$PID_FILE")"

log_start() {
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') $1 (project: $REPO_DIR) ===" >>"$LOG"
}

notify() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"Comdirect Dashboard\"" \
    >/dev/null 2>&1 || true
}

# Every step of a start or stop is both logged and shown as a notification: the
# bundles are launched from Spotlight, so the log is the only other feedback and
# nobody watches it.
step() {
  echo "$1" >>"$LOG"
  notify "$1"
}

fail() {
  echo "ERROR: $1" >>"$LOG"
  /usr/bin/osascript -e "display dialog \"$1\" with title \"Comdirect Dashboard\" \
    buttons {\"Open log\", \"OK\"} default button \"OK\" with icon caution giving up after 300" \
    -e 'if button returned of result is "Open log" then do shell script "open -a Console '"$LOG"'"' \
    >/dev/null 2>&1 || true
  exit 1
}

# It is not enough that something answers on the port: on a developer machine another
# service may sit there. The dashboard's own start page is checked instead.
reachable() {
  /usr/bin/curl -fsS -m 3 -o /dev/null "$URL/" 2>/dev/null
}

# Occupied, but by something else?
port_taken_by_foreign_service() {
  /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! reachable
}

# PID of the uvicorn process started by launcher.sh, if it is still alive.
running_pid() {
  local pid
  [ -f "$PID_FILE" ] || return 1
  pid="$(cat "$PID_FILE" 2>/dev/null)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  echo "$pid"
}

# The container runtime is whatever this machine happens to use: Colima on the
# developer machine, Docker Desktop elsewhere. Both are tried, in that order, because
# Colima leaves no app bundle behind that "open -a Docker" could find.
ensure_docker() {
  if docker info >/dev/null 2>&1; then
    step "Container runtime already running."
    return 0
  fi
  step "Starting the container runtime …"

  if command -v colima >/dev/null 2>&1; then
    # colima start blocks until the VM is up, so no polling loop is needed here. It can
    # take a minute on the first start after a reboot.
    colima start >>"$LOG" 2>&1 \
      || fail "Colima could not be started. Details in the log: $LOG"
  elif [ -d /Applications/Docker.app ] || [ -d "$HOME/Applications/Docker.app" ]; then
    open -a Docker >/dev/null 2>&1 \
      || fail "Docker Desktop could not be started. Please start it manually."
  else
    fail "No container runtime found. Install Colima (brew install colima) or Docker Desktop, or start it manually."
  fi

  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      step "Container runtime is ready."
      return 0
    fi
    sleep 2
  done
  fail "The container runtime is not ready after two minutes. Details in the log: $LOG"
}
