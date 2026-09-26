# Discord FutBin Bot

Ein Discord-Bot zur Überwachung von FIFA-Spielerpreisen auf FutBin.

## Struktur

```text
bot/
├── discord_bot.py          # Haupt-Bot-Code
├── requirements.txt        # Python-Dependencies
├── README.md              # Diese Datei
├── config/                # Konfigurationsdateien
│   ├── bot_config.yaml    # Bot-Konfiguration
│   └── discloud.config    # Deploy-Konfiguration
├── data/                  # Daten und Zustandsdateien
│   ├── player_state.json  # Spielerzustand
│   └── *.backup_*         # Automatische Backups
├── logs/                  # Log-Dateien
│   └── bot.log           # Bot-Logs
└── tests/                 # Test-Dateien
    ├── test_bot.py        # Bot-Tests
    ├── test_parsing.py    # Parser-Tests
    ├── quick_test.py      # Schnelltests
    ├── validate_system.py # Systemvalidierung
    └── test_data/         # Test-Daten
        └── test_state.json
```

## Installation

1. Installiere Dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Konfiguriere den Bot in `config/bot_config.yaml`

3. Starte den Bot:
   ```bash
   python discord_bot.py
   ```

## Konfiguration

- Bot-Einstellungen: `config/bot_config.yaml`
- Deploy-Einstellungen: `config/discloud.config`
- Spielerdaten: `data/player_state.json`

## Tests

Alle Test-Dateien befinden sich im `tests/` Ordner:
- `test_bot.py` - Bot-Funktionalitätstests
- `test_parsing.py` - Parser-Tests
- `validate_system.py` - Systemvalidierung