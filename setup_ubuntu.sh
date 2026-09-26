#!/usr/bin/env bash
# ==============================================================================
# FUTBin Discord Bot - Ubuntu Setup & Installation Script
# Supports: Ubuntu 20.04, 22.04, 24.04 LTS
# ==============================================================================

set -e

echo "🚀 Starte FUTBin Discord Bot Setup für Ubuntu..."

# 1. Systempakete aktualisieren und Abhängigkeiten installieren
echo "📦 Installiere erforderliche Systempakete..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv python3-dev build-essential

# 2. Virtuelle Umgebung (.venv) anlegen
if [ ! -d ".venv" ]; then
    echo "🐍 Erstelle virtuelle Python-Umgebung (.venv)..."
    python3 -m venv .venv
else
    echo "ℹ️ Virtuelle Umgebung (.venv) existiert bereits."
fi

# 3. Pip aktualisieren und Python-Abhängigkeiten installieren
echo "📥 Installiere Python-Abhängigkeiten aus requirements.txt..."
./.venv/bin/python -m pip install --upgrade pip setuptools wheel
./.venv/bin/pip install -r requirements.txt

# 4. Verzeichnisse sicherstellen
echo "📁 Erstelle Verzeichnisstruktur (logs, data, backups, config)..."
mkdir -p logs data backups config

# 5. .env Konfigurationsdatei prüfen
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "⚙️ Erstelle .env aus .env.example..."
        cp .env.example .env
        echo "⚠️ WICHTIG: Bitte passe deine .env Datei mit deinem DISCORD_BOT_TOKEN an!"
    fi
fi

# 6. Ausführungsrechte für Skripte vergeben
chmod +x setup_ubuntu.sh start.sh 2>/dev/null || true

echo "✅ Setup erfolgreich abgeschlossen!"
echo "➡️ Starte den Bot mit: ./start.sh"
echo "➡️ Oder richte den systemd-Dienst ein (siehe futbin-bot.service)."
