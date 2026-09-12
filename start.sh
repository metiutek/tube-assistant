#!/usr/bin/env bash
# Start TubeAssistant in background (Linux/macOS).
# Requires the CLI installed: `uv tool install git+https://github.com/metiu1/tube-assistant.git`
# or, from a clone, `uv tool install -e .`
set -e
if ! command -v tube-assistant >/dev/null 2>&1; then
    echo "[!] tube-assistant not found in PATH. Install it first (see README)." >&2
    exit 1
fi
WS="$(tube-assistant workspace)"
mkdir -p "$WS/logs"
nohup tube-assistant start > "$WS/logs/agent.log" 2>&1 &
echo "Agent started (PID $!). Logs: $WS/logs/agent.log"
