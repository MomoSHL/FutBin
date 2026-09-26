# 🐧 FUTBin Discord Bot – Ubuntu Installations- & Betriebsanleitung

Diese Anleitung führt dich Schritt für Schritt durch die Einrichtung des Bots auf **Ubuntu (20.04, 22.04 oder 24.04 LTS)**.

---

## 📋 Voraussetzungen auf dem Ubuntu-Server

* Ein Ubuntu Server / VPS oder lokales Ubuntu-System mit `sudo`-Rechten.
* Dein Discord-Bot-Token sowie die ID des Discord-Channels für das Dashboard.

---

## 🚀 Option 1: Native Installation & 24/7-Betrieb mit `systemd` (Empfohlen)

### 1. Projekt auf den Ubuntu-Server übertragen
Falls du Git verwendest:
```bash
git clone <DEIN_REPO_URL> botBin
cd botBin
```
Oder per SCP / SFTP in deinen Benutzerordner kopieren (z. B. `/home/ubuntu/botBin`).

---

### 2. Automatische Installation ausführen
Führe das vorbereitete Installationsskript aus:
```bash
chmod +x setup_ubuntu.sh start.sh
./setup_ubuntu.sh
```
Das Skript installiert automatisch:
* `python3`, `python3-pip`, `python3-venv`, `python3-dev`, `build-essential`
* Erstellt eine isolierte virtuelle Umgebung (`.venv`)
* Installiert alle Abhängigkeiten (`discord.py`, `aiohttp`, `requests`, `pydantic-settings`, etc.)
* Erstellt alle erforderlichen Ordner (`logs`, `data`, `backups`, `config`)

---

### 3. Konfiguration anpassen (`.env`)
Öffne die `.env`-Datei und trage deinen Discord-Bot-Token ein:
```bash
nano .env
```
Wichtigste Einstellungen:
```ini
DISCORD_BOT_TOKEN=Dein_Bot_Token_Hier
DISCORD_DASHBOARD_CHANNEL_ID=Deine_Channel_ID_Hier
FUTBIN_PLATFORM=pc
```
Speichern mit `STRG + O`, `ENTER` und beenden mit `STRG + X`.

---

### 4. Testlauf im Terminal
Teste kurz, ob der Bot sauber startet:
```bash
./start.sh
```
Wenn der Bot online geht und das Dashboard initialisiert, beende ihn mit `STRG + C`.

---

### 5. Als 24/7-Hintergrunddienst einrichten (`systemd`)
Damit der Bot automatisch beim Serverstart hochfährt und bei Abstürzen neustartet:

1. Passe in der Datei `futbin-bot.service` deinen Benutzernamen und Pfad an (falls dein Pfad nicht `/home/ubuntu/botBin` ist):
   ```bash
   nano futbin-bot.service
   ```
2. Kopiere die Service-Datei in das Systemverzeichnis:
   ```bash
   sudo cp futbin-bot.service /etc/systemd/system/futbin-bot.service
   ```
3. Lade die Systemd-Konfiguration neu:
   ```bash
   sudo systemctl daemon-reload
   ```
4. Aktiviere den Autostart beim Booten:
   ```bash
   sudo systemctl enable futbin-bot
   ```
5. Starte den Bot:
   ```bash
   sudo systemctl start futbin-bot
   ```

#### Nützliche Befehle zur Verwaltung:
* **Status prüfen:** `sudo systemctl status futbin-bot`
* **Live-Logs ansehen:** `journalctl -u futbin-bot -f`
* **Bot neustarten:** `sudo systemctl restart futbin-bot`
* **Bot stoppen:** `sudo systemctl stop futbin-bot`

---

## 🐳 Option 2: Betrieb mit Docker & Docker-Compose

Wenn du Docker auf deinem Ubuntu-Server nutzt:

1. Konfiguriere die `.env`-Datei:
   ```bash
   cp .env.example .env
   nano .env
   ```
2. Starte den Container im Hintergrund:
   ```bash
   docker compose up -d --build
   ```
3. Logs in Echtzeit ansehen:
   ```bash
   docker compose logs -f
   ```
4. Container stoppen / neustarten:
   ```bash
   docker compose restart
   docker compose down
   ```

---

## 🔍 Wichtige Hinweise für Ubuntu

1. **Cloudflare & Scraping:**
   Der Bot nutzt den integrierten automatischen Request-Bypass in `services/http_client.py`. Es werden keine speziellen Browser oder GUI-Pakete benötigt – der Bot läuft als reiner Headless-Server-Dienst.

2. **Dateiberechtigungen:**
   Stelle sicher, dass der ausführende Benutzer Schreibrechte auf die Ordner `data/`, `logs/` und `config/` hat:
   ```bash
   chmod -R u+rwX config data logs backups
   ```

3. **Gesundheitsprüfung (Health Check):**
   Der Bot stellt unter Port `8080` (oder dem in `.env` konfigurierten `PORT`) einen `/health`-Endpunkt bereit:
   ```bash
   curl http://localhost:8080/health
   ```
   Rückgabe bei laufendem Bot: `{"status": "healthy", ...}`.
