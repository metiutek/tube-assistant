#!/bin/bash
# TubeAssistant — Linux/Raspberry Pi installer (from a git clone)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "=== TubeAssistant — Setup ==="
echo ""

# python check
if ! command -v python3 &>/dev/null; then
    echo "[!] python3 not found. Install with: sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

# ffmpeg check
if ! command -v ffmpeg &>/dev/null; then
    echo "[!] ffmpeg not found. Installing..."
    sudo apt-get update && sudo apt-get install -y ffmpeg
fi

# venv
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "[*] Installing TubeAssistant (editable) ..."
pip install --upgrade pip -q
pip install -e . -q

echo ""
echo "[✓] Installation complete."
echo ""

# alias: activates the venv and opens the interactive menu
SHELL_RC="$HOME/.bashrc"
[ -n "$ZSH_VERSION" ] && SHELL_RC="$HOME/.zshrc"

ALIAS_LINE="alias tube-assistant='cd \"$SCRIPT_DIR\" && source venv/bin/activate && tube-assistant'"
if ! grep -q "alias tube-assistant=" "$SHELL_RC" 2>/dev/null; then
    echo "$ALIAS_LINE" >> "$SHELL_RC"
    echo "[✓] Added 'tube-assistant' alias to $SHELL_RC"
    echo "    Run: source $SHELL_RC"
fi

echo "[*] Starting setup wizard..."
echo ""
tube-assistant onboard
