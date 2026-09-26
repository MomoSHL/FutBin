#!/usr/bin/env bash
# ==============================================================================
# FUTBin Discord Bot - Start Script for Ubuntu / Linux
# ==============================================================================

set -e

# Change directory to the bot's folder
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if .venv exists
if [ ! -d ".venv" ]; then
    echo "❌ Virtuelle Umgebung (.venv) nicht gefunden! Bitte führe zuerst ./setup_ubuntu.sh aus."
    exit 1
fi

# Ensure directories exist
mkdir -p logs data backups config

echo "🚀 Starte FUTBin Discord Bot..."
exec ./.venv/bin/python main.py
