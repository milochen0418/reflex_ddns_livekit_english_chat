#!/bin/bash

# 3000/8000: Reflex frontend/backend.
# 7680: the embedded livekit-server, which runs detached so it survives hot
#       reloads; stop it too so the restarted backend starts a fresh one.
echo "Checking for processes on ports 3000, 8000 and 7680..."

PIDS=$(lsof -ti:3000,8000,7680)

if [ -n "$PIDS" ]; then
  echo "Killing processes: $PIDS"
  kill -9 $PIDS
  echo "Processes killed."
else
  echo "No processes found on ports 3000, 8000 or 7680."
fi

echo "Starting Reflex app with args: $@"

# If this script is executed via `poetry run`, avoid nesting Poetry.
if [ -n "${POETRY_ACTIVE:-}" ]; then
  reflex run "$@"
else
  poetry run reflex run "$@"
fi
