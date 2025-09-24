import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime
import time
import json
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import yaml
import gc  # Garbage Collector für Memory-Management
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Health Check HTTP Server für Deployment-Plattformen
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                'status': 'healthy',
                'service': 'FutBin Discord Bot',
                'timestamp': datetime.now().isoformat(),
                'bot_ready': hasattr(bot, 'user') and bot.user is not None if 'bot' in globals() else False
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress default HTTP server logs
        pass

def start_health_server():
    """Startet einen HTTP-Server für Health Checks"""
    try:
        port = int(os.environ.get('PORT', 8080))  # Verwendet PORT env var oder default 8080
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logging.info(f"Health Check Server gestartet auf Port {port}")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Health Check Server konnte nicht gestartet werden: {e}")

###############################################
# KONFIGURATION
###############################################

BOT_TOKEN = "MTQxOTMwMjc3NzQ5NDA0ODc2OA.G9vPX_.TLBWBZLqxMKXMZdLozX1ZSqvMTsfCQ1d8DrgYQ"


# FutBin Tracking Konfiguration
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = "config/bot_config.yaml"
STATE_FILE = "data/player_state.json"

# Netzwerk Konfiguration
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
REQUEST_TIMEOUT = 12.0
RETRY_COUNT = 2
SLEEP_BETWEEN = 1

# Dashboard Konfiguration
DASHBOARD_UPDATE_INTERVAL = 300  # 5 Minuten in Sekunden (erhöht für Discloud)
PRICE_CHECK_INTERVAL = 120      # 2 Minuten zwischen Preis-Checks (erhöht)
DASHBOARD_CHANNEL_ID = None     # Wird über Command gesetzt

# Nachrichten-Cleanup Konfiguration
TEMP_MESSAGE_DELETE_AFTER = 10  # Temporäre Nachrichten nach 10 Sekunden löschen
COMMAND_RESPONSE_DELETE_AFTER = 5  # Command-Antworten nach 5 Sekunden löschen

# Memory-Management
import gc  # Garbage Collector für Memory-Management

# Semaphore für Preis-Checks (verhindert gleichzeitige Requests)
price_check_semaphore = asyncio.Semaphore(1)

# HTTP Session Management (verhindert Memory Leaks)
import requests.adapters
requests.adapters.DEFAULT_POOLBLOCK = True
requests.adapters.DEFAULT_POOLSIZE = 5  # Reduzierte Pool-Größe
requests.adapters.DEFAULT_RETRIES = 1

# Nachrichten-Cleanup Hilfsfunktion
async def delete_message_after_delay(message, delay_seconds=TEMP_MESSAGE_DELETE_AFTER):
    """Löscht eine Nachricht nach einer bestimmten Zeit"""
    try:
        await asyncio.sleep(delay_seconds)
        await message.delete()
    except discord.NotFound:
        pass  # Nachricht bereits gelöscht
    except discord.Forbidden:
        logging.warning("Keine Berechtigung zum Löschen der Nachricht")
    except Exception as e:
        logging.error(f"Fehler beim Löschen der Nachricht: {e}")

# Farben für Embeds
COLOR_UP = 0x2ecc71       # Grün für Preisanstieg
COLOR_DOWN = 0xe74c3c     # Rot für Preisfall
COLOR_NEUTRAL = 0x95a5a6  # Grau für keine Änderung
COLOR_BOT = 0xfa7100      # Orange für Bot-Farbe

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"}
###############################################

# Dashboard Views und Buttons
class DashboardView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)  # Persistent View
        self.bot = bot_instance

    @discord.ui.button(label="➕ Spieler", style=discord.ButtonStyle.green, custom_id="add_player")
    async def add_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddPlayerModal(self.bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🗑️ Entfernen", style=discord.ButtonStyle.red, custom_id="remove_player")
    async def remove_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.bot.players:
            embed = discord.Embed(
                title="❌ Keine Spieler vorhanden",
                description="Es sind keine Spieler zum Entfernen vorhanden.",
                color=COLOR_DOWN
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        view = RemovePlayerView(self.bot)
        embed = discord.Embed(
            title="🗑️ Spieler entfernen",
            description="Wähle einen Spieler zum Entfernen:",
            color=COLOR_NEUTRAL
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        
    @discord.ui.button(label="🚨 Alerts", style=discord.ButtonStyle.blurple, custom_id="manage_alerts")
    async def manage_alerts_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Zeigt Alert-Management Interface"""
        view = AlertManagementView(self.bot, interaction.user.id)
        embed = discord.Embed(
            title="🚨 Alert-Management",
            description="Verwalte deine Preis-Alerts für überwachte Spieler.",
            color=COLOR_BOT
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🔄 Aktualisieren", style=discord.ButtonStyle.gray, custom_id="dashboard_refresh_button")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prüfe, ob die Interaktion bereits beantwortet wurde
        if interaction.response.is_done():
            return
            
        await interaction.response.defer()
        
        # Führe Preis-Update durch (respektiere Semaphore)
        if price_check_semaphore.locked():
            embed = discord.Embed(
                title="⏳ Preis-Check läuft bereits",
                description="Ein Preis-Check ist bereits in Bearbeitung. Bitte warte einen Moment.",
                color=COLOR_NEUTRAL
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        await self.bot.price_checker()
        
        # Aktualisiere Dashboard
        await self.bot.update_dashboard()
        
        embed = discord.Embed(
            title="✅ Dashboard aktualisiert",
            description="🔄 Alle Preise wurden neu geladen!\n📊 Dashboard wurde aktualisiert!",
            color=COLOR_UP
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
class AlertManagementView(discord.ui.View):
    def __init__(self, bot_instance, user_id):
        super().__init__(timeout=300)
        self.bot = bot_instance
        self.user_id = user_id

    @discord.ui.button(label="➕ Alert erstellen", style=discord.ButtonStyle.green)
    async def create_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CreateAlertModal(self.bot, self.user_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="📋 Meine Alerts", style=discord.ButtonStyle.blurple)
    async def list_alerts(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_alerts = [p for p in self.bot.players if getattr(p, 'alert_user_id', None) == self.user_id]
        
        if not user_alerts:
            embed = discord.Embed(
                title="📋 Deine Alerts",
                description="❌ Du hast noch keine Alerts gesetzt.",
                color=COLOR_NEUTRAL
            )
        else:
            embed = discord.Embed(
                title=f"📋 Deine Alerts ({len(user_alerts)})",
                color=COLOR_BOT
            )
            
            for i, player in enumerate(user_alerts, 1):
                state = self.bot.state.get(player.url, {})
                current_price = state.get('price', 'Unbekannt')
                
                field_value = f"**Aktuell**: `{current_price:,}` Coins\n" if isinstance(current_price, int) else "**Aktuell**: Unbekannt\n"
                
                if getattr(player, 'alert_above', None):
                    status = "🟢" if isinstance(current_price, int) and current_price >= player.alert_above else "🔴"
                    field_value += f"{status} **Über**: `{player.alert_above:,}` Coins\n"
                
                if getattr(player, 'alert_below', None):
                    status = "🟢" if isinstance(current_price, int) and current_price <= player.alert_below else "🔴"
                    field_value += f"{status} **Unter**: `{player.alert_below:,}` Coins\n"
                
                field_value += f"📁 **Kategorie**: {getattr(player, 'category', 'Allgemein')}"
                
                embed.add_field(
                    name=f"{i}. {player.name}",
                    value=field_value,
                    inline=True
                )
        
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🗑️ Alert löschen", style=discord.ButtonStyle.red)
    async def delete_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_alerts = [p for p in self.bot.players if getattr(p, 'alert_user_id', None) == self.user_id]
        
        if not user_alerts:
            embed = discord.Embed(
                title="❌ Keine Alerts",
                description="Du hast keine Alerts zum Löschen.",
                color=COLOR_DOWN
            )
            await interaction.response.edit_message(embed=embed, view=self)
            return
        
        view = DeleteAlertView(self.bot, self.user_id, user_alerts)
        embed = discord.Embed(
            title="🗑️ Alert löschen",
            description="Wähle einen Alert zum Löschen:",
            color=COLOR_NEUTRAL
        )
        await interaction.response.edit_message(embed=embed, view=view)

class CreateAlertModal(discord.ui.Modal):
    def __init__(self, bot_instance, user_id):
        super().__init__(title="🚨 Preis-Alert erstellen")
        self.bot = bot_instance
        self.user_id = user_id

    player_name = discord.ui.TextInput(
        label="Spielername",
        placeholder="z.B. Harry Kane",
        style=discord.TextStyle.short,
        required=True
    )
    
    price = discord.ui.TextInput(
        label="Preis (z.B. 50k, 1.5m, 32000)",
        placeholder="z.B. 50k für 50.000 Coins",
        style=discord.TextStyle.short,
        required=True
    )
    
    alert_type = discord.ui.TextInput(
        label="Alert-Typ (über/unter)",
        placeholder="über = benachrichtigen bei Erreichen/Überschreitung, unter = bei Unterschreitung",
        style=discord.TextStyle.short,
        required=True,
        default="über"
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            # Parse Preis-Input
            parsed_price = self.bot.parse_alert_price(str(self.price.value))
            if parsed_price is None or parsed_price <= 0:
                embed = discord.Embed(
                    title="❌ Ungültiger Preis",
                    description=f"**{self.price.value}** ist kein gültiger Preis.\n\n**Beispiele:**\n• `50k` = 50.000 Coins\n• `1.5m` = 1.500.000 Coins\n• `250000` = 250.000 Coins",
                    color=COLOR_DOWN
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Parse Alert-Typ
            alert_type_input = str(self.alert_type.value).lower().strip()
            is_above_alert = True  # Default
            
            if alert_type_input in ['unter', 'below', 'less', '<', 'kleiner']:
                is_above_alert = False
            elif alert_type_input in ['über', 'ueber', 'above', 'more', '>', 'größer', 'groesser']:
                is_above_alert = True
            else:
                embed = discord.Embed(
                    title="❌ Ungültiger Alert-Typ",
                    description=f"**{self.alert_type.value}** ist kein gültiger Alert-Typ.\n\n**Gültige Werte:**\n• `über` - benachrichtigen wenn Preis erreicht/überschritten wird\n• `unter` - benachrichtigen wenn Preis unterschritten wird",
                    color=COLOR_DOWN
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Finde Spieler
            player_found = None
            for player in self.bot.players:
                if player.name.lower() == str(self.player_name.value).lower():
                    player_found = player
                    break
            
            if not player_found:
                embed = discord.Embed(
                    title="❌ Spieler nicht gefunden",
                    description=f"**{self.player_name.value}** wird nicht überwacht.\n\nVerwende `/list_players` um alle Spieler zu sehen.",
                    color=COLOR_DOWN
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Setze Alert basierend auf Typ
            if is_above_alert:
                player_found.alert_above = parsed_price
                player_found.alert_below = None
                alert_description = f"Du wirst benachrichtigt wenn **{player_found.name}** {parsed_price:,} Coins erreicht oder überschreitet."
            else:
                player_found.alert_below = parsed_price
                player_found.alert_above = None
                alert_description = f"Du wirst benachrichtigt wenn **{player_found.name}** unter {parsed_price:,} Coins fällt."
                
            player_found.alert_user_id = self.user_id
            
            # Aktualisiere Config
            for config_player in self.bot.config.get('players', []):
                if config_player.get('url') == player_found.url:
                    if is_above_alert:
                        config_player['alert_above'] = parsed_price
                        config_player['alert_below'] = None
                    else:
                        config_player['alert_below'] = parsed_price  
                        config_player['alert_above'] = None
                    config_player['alert_user_id'] = self.user_id
                    break
            
            self.bot._save_config()
            
            # Erfolgsmeldung
            current_price = self.bot.state.get(player_found.url, {}).get('price', 0)
            
            if is_above_alert:
                status_emoji = "🟢" if isinstance(current_price, int) and current_price >= parsed_price else "🔴"
                alert_info = f"📈 **Alert bei**: Über {parsed_price:,} Coins"
            else:
                status_emoji = "🟢" if isinstance(current_price, int) and current_price <= parsed_price else "🔴"
                alert_info = f"📉 **Alert bei**: Unter {parsed_price:,} Coins"
            
            embed = discord.Embed(
                title="✅ Alert erfolgreich gesetzt!",
                description=alert_description,
                color=COLOR_UP
            )
            
            embed.add_field(
                name="📊 Status",
                value=f"{status_emoji} **Aktuell**: {current_price:,} Coins\n{alert_info}",
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Aktualisiere Dashboard
            await self.bot.update_dashboard()
            
        except Exception as e:
            logging.error(f"Fehler beim Erstellen des Alerts: {e}")
            embed = discord.Embed(
                title="❌ Fehler beim Alert erstellen",
                description=f"Ein unerwarteter Fehler ist aufgetreten: {str(e)}",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

class DeleteAlertView(discord.ui.View):
    def __init__(self, bot_instance, user_id, user_alerts):
        super().__init__(timeout=60)
        self.bot = bot_instance
        self.user_id = user_id
        
        # Erstelle Dropdown
        options = []
        for i, player in enumerate(user_alerts[:25]):
            alerts = []
            if getattr(player, 'alert_above', None):
                alerts.append(f"Über {player.alert_above:,}")
            if getattr(player, 'alert_below', None):
                alerts.append(f"Unter {player.alert_below:,}")
            
            options.append(discord.SelectOption(
                label=player.name,
                value=str(i),
                description=f"Alerts: {' | '.join(alerts)}"
            ))
        
        if options:
            self.select_alert.options = options

    @discord.ui.select(placeholder="Wähle einen Alert zum Löschen...")
    async def select_alert(self, interaction: discord.Interaction, select: discord.ui.Select):
        user_alerts = [p for p in self.bot.players if getattr(p, 'alert_user_id', None) == self.user_id]
        alert_index = int(select.values[0])
        player_to_clear = user_alerts[alert_index]
        
        # Entferne Alerts
        old_alerts = []
        if getattr(player_to_clear, 'alert_above', None):
            old_alerts.append(f"Über: `{player_to_clear.alert_above:,}` Coins")
        if getattr(player_to_clear, 'alert_below', None):
            old_alerts.append(f"Unter: `{player_to_clear.alert_below:,}` Coins")
        
        player_to_clear.alert_above = None
        player_to_clear.alert_below = None
        player_to_clear.alert_user_id = None
        
        # Aktualisiere Konfiguration
        for config_player in self.bot.config.get('players', []):
            if config_player.get('url') == player_to_clear.url:
                config_player.pop('alert_above', None)
                config_player.pop('alert_below', None)
                config_player.pop('alert_user_id', None)
                break
        
        self.bot._save_config()
        
        embed = discord.Embed(
            title="✅ Alert gelöscht",
            description=f"**{player_to_clear.name}**\n\nEntfernte Alerts:\n" + "\n".join(old_alerts),
            color=COLOR_UP
        )
        
        await interaction.response.edit_message(embed=embed, view=None)

class AddPlayerModal(discord.ui.Modal):
    def __init__(self, bot_instance):
        super().__init__(title="Spieler hinzufügen")
        self.bot = bot_instance

    url = discord.ui.TextInput(
        label="FutBin URL",
        placeholder="https://www.futbin.com/26/player/234/viktor-gyokeres",
        style=discord.TextStyle.short,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        futbin_url = self.url.value.strip()
        
        # Validiere URL
        if not futbin_url.startswith("https://www.futbin.com/"):
            embed = discord.Embed(
                title="❌ Ungültige URL",
                description="Die URL muss mit `https://www.futbin.com/` beginnen.",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Prüfe ob URL bereits existiert
        for player in self.bot.players:
            if player.url == futbin_url:
                embed = discord.Embed(
                    title="❌ Spieler bereits vorhanden",
                    description=f"**{player.name}** wird bereits überwacht.",
                    color=COLOR_DOWN
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
        
        try:
            # Lade Seite und extrahiere Daten
            html = await self.bot.fetch_player_page(futbin_url)
            if not html:
                embed = discord.Embed(
                    title="❌ URL nicht erreichbar",
                    description="Die angegebene URL konnte nicht erreicht werden.",
                    color=COLOR_DOWN
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Parse Spielerdaten
            parsed = self.bot.parse_price_and_image(html)
            player_name = parsed.get('name')
            price = parsed.get('price')
            
            if not player_name:
                # Fallback: Name aus URL extrahieren
                import re
                match = re.search(r'/([^/]+)/?$', futbin_url.rstrip('/'))
                if match:
                    player_name = match.group(1).replace('-', ' ').title()
                else:
                    player_name = "Unbekannter Spieler"
            
            # Füge Spieler hinzu
            new_player = PlayerConfig(name=player_name, url=futbin_url, active=True)
            self.bot.players.append(new_player)
            
            # Aktualisiere Konfiguration
            if 'players' not in self.bot.config:
                self.bot.config['players'] = []
            
            self.bot.config['players'].append({
                'name': player_name,
                'url': futbin_url,
                'active': True
            })
            
            self.bot._save_config()
            
            # Erstelle initialen State-Eintrag
            if price:
                self.bot.update_state(new_player, price, parsed.get('image'))
                self.bot._save_state()
            
            embed = discord.Embed(
                title="✅ Spieler hinzugefügt",
                description=f"**{player_name}** wird jetzt überwacht!\n{f'Aktueller Preis: `{price:,}` Coins' if price else 'Preis wird beim nächsten Update geholt'}",
                color=COLOR_UP
            )
            
            if parsed.get('image'):
                embed.set_thumbnail(url=parsed['image'])
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Aktualisiere Dashboard
            if self.bot.dashboard_channel:
                await self.bot.update_dashboard()
        
        except Exception as e:
            embed = discord.Embed(
                title="❌ Fehler beim Hinzufügen",
                description=f"Fehler: {str(e)}",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

class RemovePlayerView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=60)
        self.bot = bot_instance
        
        # Erstelle Dropdown mit Spielern
        options = []
        for i, player in enumerate(self.bot.players[:25]):  # Discord limit: 25 options
            options.append(discord.SelectOption(
                label=player.name,
                value=str(i),
                description=f"URL: {player.url[-30:]}"  # Letzten 30 Zeichen der URL
            ))
        
        if options:
            self.select_player.options = options

    @discord.ui.select(placeholder="Wähle einen Spieler zum Entfernen...")
    async def select_player(self, interaction: discord.Interaction, select: discord.ui.Select):
        player_index = int(select.values[0])
        player_to_remove = self.bot.players[player_index]
        
        # Entferne aus der Liste
        self.bot.players.remove(player_to_remove)
        
        # Entferne aus Konfiguration
        self.bot.config['players'] = [
            p for p in self.bot.config.get('players', []) 
            if p.get('url') != player_to_remove.url
        ]
        self.bot._save_config()
        
        # Entferne aus State
        if player_to_remove.url in self.bot.state:
            del self.bot.state[player_to_remove.url]
            self.bot._save_state()
        
        embed = discord.Embed(
            title="✅ Spieler entfernt",
            description=f"**{player_to_remove.name}** wird nicht mehr überwacht.",
            color=COLOR_UP
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Aktualisiere Dashboard
        if self.bot.dashboard_channel:
            await self.bot.update_dashboard()

@dataclass
class PlayerConfig:
    name: str
    url: str
    active: bool = True
    threshold_percent: Optional[float] = None
    # Neue Alert-Felder
    alert_above: Optional[int] = None  # Alert wenn Preis über diesem Wert
    alert_below: Optional[int] = None  # Alert wenn Preis unter diesem Wert
    alert_user_id: Optional[int] = None  # Discord User ID für Ping
    category: str = "Allgemein"  # Kategorie für Organization
    notes: str = ""  # Notizen zum Spieler

class FutBinBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True  # Für Server-Zugriff
        intents.guild_messages = True  # Für Nachrichten in Servern
        intents.members = True  # Für Member-Liste (optional)
        super().__init__(
            command_prefix='!', 
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.watching, name="FutBin Preise 📊")
        )
        
        # Bot State
        self.config_path = (BASE_DIR / CONFIG_FILE).resolve()
        self.state_path = (BASE_DIR / STATE_FILE).resolve()
        self.dashboard_message_id = None
        self.dashboard_channel = None
        
        # Tracker State
        self.config = {}
        self.players: List[PlayerConfig] = []
        self.state: Dict[str, Any] = {}
        self.global_threshold = 5.0
        
        # Task Health Monitoring
        self.last_price_check = None
        self.last_dashboard_update = None
        self.task_errors = {"price_checker": 0, "dashboard_updater": 0}
        
        self._ensure_config_exists()
        self._load_config()
        self._load_state()

    def _ensure_config_exists(self):
        """Erstellt eine Standard-Konfigurationsdatei wenn sie nicht existiert"""
        if not self.config_path.exists():
            logging.warning("Bot-Konfigurationsdatei fehlt – erstelle Beispiel.")
            sample = {
                'settings': {
                    'platform': 'ps',
                    'global_threshold_percent': 5,
                    'dashboard_channel_id': None
                },
                'players': [
                    {"name": "Harry Kane", "url": "https://www.futbin.com/26/player/18919/harry-kane", "active": True},
                ]
            }
            try:
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    yaml.safe_dump(sample, f, allow_unicode=True, sort_keys=False)
            except Exception as e:
                logging.error(f"Konnte Konfig nicht schreiben: {e}")

    def _load_config(self):
        """Lädt die Bot-Konfiguration"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}
        except Exception as e:
            logging.error(f"Fehler beim Laden der Konfig: {e}")
            self.config = {}
        
        settings = self.config.get('settings', {})
        self.global_threshold = float(settings.get('global_threshold_percent', 5))
        global DASHBOARD_CHANNEL_ID
        DASHBOARD_CHANNEL_ID = settings.get('dashboard_channel_id')
        
        # Lade Dashboard-Message-ID falls vorhanden
        self.dashboard_message_id = settings.get('dashboard_message_id')
        if self.dashboard_message_id:
            logging.info(f"Dashboard-Message-ID aus Config geladen: {self.dashboard_message_id}")
        
        self.players = [
            PlayerConfig(
                name=p.get('name', ''),
                url=p.get('url', ''),
                active=p.get('active', True),
                threshold_percent=p.get('threshold_percent'),
                alert_above=p.get('alert_above'),
                alert_below=p.get('alert_below'),
                alert_user_id=p.get('alert_user_id'),
                category=p.get('category', 'Allgemein'),
                notes=p.get('notes', '')
            ) for p in self.config.get('players', []) 
            if p.get('active', True)
        ]

    def _save_config(self):
        """Speichert die aktuelle Konfiguration"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(self.config, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logging.error(f"Fehler beim Speichern der Konfig: {e}")

    def _load_state(self):
        """Lädt den persistenten Zustand"""
        if self.state_path.exists():
            try:
                with open(self.state_path, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
            except Exception as e:
                logging.warning(f"Konnte State nicht laden: {e}")
                self.state = {}
        else:
            self.state = {}

    def _save_state(self):
        """Speichert den aktuellen Zustand"""
        try:
            tmp = str(self.state_path) + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            Path(tmp).replace(self.state_path)
        except Exception as e:
            logging.error(f"Fehler beim Speichern des States: {e}")

    async def on_ready(self):
        """Event wenn der Bot bereit ist"""
        logging.info(f'{self.user} ist online und bereit!')
        logging.info(f'Bot ID: {self.user.id}')
        logging.info(f'Verbunden mit {len(self.guilds)} Server(n)')
        
        # Zeige Server-Informationen
        for guild in self.guilds:
            logging.info(f'- Server: {guild.name} (ID: {guild.id}, Members: {guild.member_count})')
        
        # Registriere persistent Views
        self.add_view(DashboardView(self))
        logging.info("Dashboard Views registriert")
        
        # Lade Dashboard Channel wenn gesetzt
        if DASHBOARD_CHANNEL_ID:
            try:
                self.dashboard_channel = self.get_channel(DASHBOARD_CHANNEL_ID)
                if self.dashboard_channel:
                    logging.info(f"Dashboard Channel geladen: {self.dashboard_channel.name} in {self.dashboard_channel.guild.name}")
                    
                    # Teste Bot-Permissions im Channel
                    permissions = self.dashboard_channel.permissions_for(self.dashboard_channel.guild.me)
                    logging.info(f"Bot Permissions im Dashboard Channel:")
                    logging.info(f"- Send Messages: {permissions.send_messages}")
                    logging.info(f"- Embed Links: {permissions.embed_links}")
                    logging.info(f"- Use External Emojis: {permissions.use_external_emojis}")
                    logging.info(f"- Read Message History: {permissions.read_message_history}")
                    
                    if not permissions.send_messages:
                        logging.error("[ERROR] Bot kann keine Nachrichten in diesem Channel senden!")
                    if not permissions.embed_links:
                        logging.error("[ERROR] Bot kann keine Embeds in diesem Channel senden!")
                        
                else:
                    logging.warning(f"Dashboard Channel {DASHBOARD_CHANNEL_ID} nicht gefunden")
            except Exception as e:
                logging.error(f"Fehler beim Laden des Dashboard Channels: {e}")
        
        # Starte Background Tasks
        if not self.price_checker.is_running():
            self.price_checker.start()
            logging.info("Preis-Checker gestartet")
        if not self.dashboard_updater.is_running():
            self.dashboard_updater.start()
            logging.info("Dashboard-Updater gestartet")
        
        # Sync Commands
        try:
            synced = await self.tree.sync()
            logging.info(f"[OK] {len(synced)} Slash Command(s) synchronisiert")
            for cmd in synced:
                logging.info(f"  - /{cmd.name}: {cmd.description}")
        except Exception as e:
            logging.error(f"[ERROR] Fehler beim Synchronisieren der Commands: {e}")
    
    async def on_error(self, event_method: str, *args, **kwargs):
        """Globaler Error Handler für Discord Events"""
        logging.error(f"Discord Event Error in {event_method}: {args}, {kwargs}")
        
        # Versuche Bot zu stabilisieren
        try:
            gc.collect()  # Memory freigeben
            await asyncio.sleep(5)  # Kurze Pause
        except Exception as recovery_error:
            logging.error(f"Fehler bei Error Recovery: {recovery_error}")
    
    async def on_disconnect(self):
        """Handler für Discord Disconnections"""
        logging.warning("Bot wurde von Discord getrennt")
        
    async def on_resumed(self):
        """Handler für Discord Reconnections"""
        logging.info("Bot Verbindung zu Discord wiederhergestellt")
        
        # Teste eine einfache Nachricht im Dashboard Channel
        if self.dashboard_channel:
            try:
                test_embed = discord.Embed(
                    title="🤖 Bot Online",
                    description="FutBin Bot ist erfolgreich gestartet und bereit!",
                    color=COLOR_BOT
                )
                test_message = await self.dashboard_channel.send(embed=test_embed)
                logging.info("[OK] Test-Nachricht erfolgreich gesendet")
                
                # Lösche Test-Nachricht nach 5 Sekunden
                await asyncio.sleep(5)
                await test_message.delete()
                
                # Erstelle das eigentliche Dashboard
                await self.update_dashboard()
                logging.info("[OK] Dashboard erfolgreich erstellt")
                
            except discord.Forbidden:
                logging.error("[ERROR] Keine Berechtigung zum Senden von Nachrichten!")
            except discord.HTTPException as e:
                logging.error(f"[ERROR] HTTP Fehler beim Senden: {e}")
            except Exception as e:
                logging.error(f"❌ Fehler beim Erstellen des Dashboards: {e}")
        else:
            logging.warning("⚠️ Kein Dashboard Channel konfiguriert")

    # FutBin Tracking Methods (aus tracker.py portiert)
    async def fetch_player_page(self, url: str) -> Optional[str]:
        """Holt die HTML-Seite eines Spielers mit verbesserter Fehlerbehandlung"""
        session_headers = {
            **HEADERS,
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }
        
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                # Verwende asyncio für non-blocking request
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None, 
                    lambda: requests.get(
                        url, 
                        headers=session_headers, 
                        timeout=REQUEST_TIMEOUT,
                        allow_redirects=True
                    )
                )
                
                if response.status_code == 200:
                    # Validiere Response
                    if len(response.text) < 1000:  # Zu kurz für eine echte FutBin-Seite
                        logging.warning(f"Verdächtig kurze Antwort für {url}: {len(response.text)} chars")
                        raise requests.RequestException("Response zu kurz")
                    
                    return response.text
                
                elif response.status_code == 429:  # Rate Limited
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.warning(f"Rate Limited für {url}, warte {wait_time}s (Versuch {attempt})")
                    await asyncio.sleep(wait_time)
                    
                elif response.status_code in [502, 503, 504]:  # Server Errors
                    logging.warning(f"Server Error {response.status_code} für {url} (Versuch {attempt})")
                    await asyncio.sleep(1.5 * attempt)
                    
                else:
                    logging.warning(f"HTTP {response.status_code} für {url} (Versuch {attempt})")
                    if attempt == RETRY_COUNT:  # Letzter Versuch
                        break
                    await asyncio.sleep(1.0 * attempt)
                    
            except requests.exceptions.Timeout:
                logging.warning(f"Timeout für {url} (Versuch {attempt})")
                if attempt < RETRY_COUNT:
                    await asyncio.sleep(1.0 * attempt)
                    
            except requests.exceptions.ConnectionError:
                logging.warning(f"Verbindungsfehler für {url} (Versuch {attempt})")
                if attempt < RETRY_COUNT:
                    await asyncio.sleep(2.0 * attempt)
                    
            except requests.RequestException as e:
                logging.warning(f"Request-Fehler für {url}: {e} (Versuch {attempt})")
                if attempt < RETRY_COUNT:
                    await asyncio.sleep(1.5 * attempt)
                    
            except Exception as e:
                logging.error(f"Unerwarteter Fehler beim Abruf {url}: {e} (Versuch {attempt})")
                if attempt < RETRY_COUNT:
                    await asyncio.sleep(2.0 * attempt)
        
        return None

    def parse_price_and_image(self, html: str) -> Dict[str, Any]:
        """Extrahiert Preis, Bild-URL und Spielername mit verbesserter Fehlerbehandlung"""
        if not html or len(html) < 100:
            return {"price": None, "image": None, "name": None}
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception as e:
            logging.error(f"HTML Parse Error: {e}")
            return {"price": None, "image": None, "name": None}
        
        # Preis suchen mit mehreren Strategien
        price_value = None
        price_selectors = [
            '.price.inline-with-icon.lowest-price-1',
            '.price.lowest-price-1',
            '.lowest-price-1',
            '.price',
            '[class*="price"]'
        ]
        
        for selector in price_selectors:
            try:
                price_el = soup.select_one(selector)
                if price_el:
                    price_text = price_el.get_text(strip=True)
                    price_value = self._extract_price(price_text)
                    if price_value:
                        break
            except Exception:
                continue
        
        # Fallback: Suche im ganzen lowest-prices-wrapper
        if price_value is None:
            try:
                wrapper = soup.select_one('.lowest-prices-wrapper')
                if wrapper:
                    txt = wrapper.get_text(" ", strip=True)
                    price_value = self._extract_price(txt)
            except Exception:
                pass
        
        # Bild suchen
        img_url = None
        img_selectors = [
            '.playercard-26-base-img',
            '.playercard-base-img',
            '[class*="playercard"][class*="img"]',
            '.player-img img',
            '.card-image img'
        ]
        
        for selector in img_selectors:
            try:
                img_el = soup.select_one(selector)
                if img_el:
                    img_url = img_el.get('data-original') or img_el.get('src') or img_el.get('data-src')
                    if img_url:
                        if img_url.startswith('//'):
                            img_url = 'https:' + img_url
                        elif img_url.startswith('/'):
                            img_url = 'https://www.futbin.com' + img_url
                        break
            except Exception:
                continue
        
        # Spielername extrahieren
        player_name = None
        name_selectors = [
            '.playercard-26-name.text-ellipsis',
            '.playercard-26-name', 
            'h1.player_name',
            '.player-header h1',
            '.player-name',
            'h1',
            '.card-name',
            '[class*="player"][class*="name"]'
        ]
        
        for selector in name_selectors:
            try:
                name_el = soup.select_one(selector)
                if name_el:
                    player_name = name_el.get_text(strip=True)
                    if player_name and len(player_name) > 2:  # Mindestlänge
                        break
            except Exception:
                continue
        
        # Fallback: Aus Title extrahieren
        if not player_name:
            try:
                title_el = soup.find('title')
                if title_el:
                    title_text = title_el.get_text()
                    # Extrahiere Namen aus Titel wie "Viktor Gyökeres - 84 - FIFA 26"
                    import re
                    match = re.search(r'^([^-]+)', title_text)
                    if match:
                        player_name = match.group(1).strip()
            except Exception:
                pass
        
        return {
            "price": price_value, 
            "image": img_url,
            "name": player_name
        }

    def _extract_price(self, text: str) -> Optional[int]:
        """Extrahiert den numerischen Preis aus Text mit verbesserter Regex"""
        if not text:
            return None
        
        try:
            import re
            
            # Entferne alles außer Zahlen, Punkt, Komma, K, M
            clean_text = re.sub(r'[^\d.,KkMm\s]', '', text.strip())
            
            # Spezielle Behandlung für deutsche Zahlenformate
            # 267.000 = 267000 (deutsche Tausender-Notation)
            # 1.5M = 1500000
            # 250K = 250000
            
            # Pattern für verschiedene Formate
            patterns = [
                # Deutsche Tausender mit K/M: 1.500K, 2.5M
                r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*([KkMm])',
                # Amerikanische Tausender mit K/M: 1,500K, 2.5M  
                r'(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*([KkMm])',
                # Einfache Zahlen mit K/M: 250K, 1.5M
                r'(\d+(?:[.,]\d+)?)\s*([KkMm])',
                # Deutsche Tausender ohne Suffix: 267.000, 1.234.567
                r'(\d{1,3}(?:\.\d{3})+)(?![KkMm])',
                # Amerikanische Tausender ohne Suffix: 267,000, 1,234,567
                r'(\d{1,3}(?:,\d{3})+)(?![KkMm])',
                # Einfache Zahlen ohne Suffix
                r'(\d+)(?![.,KkMm])'
            ]
            
            for i, pattern in enumerate(patterns):
                matches = re.findall(pattern, clean_text)
                if matches:
                    for match in matches:
                        try:
                            if isinstance(match, tuple) and len(match) == 2:
                                # Match mit Suffix (K/M)
                                base_str, suffix = match
                            elif isinstance(match, tuple) and len(match) == 1:
                                # Match ohne Suffix
                                base_str, suffix = match[0], ''
                            else:
                                # Einfacher String
                                base_str, suffix = str(match), ''
                            
                            # Bestimme ob deutsche oder amerikanische Notation
                            if '.' in base_str and ',' in base_str:
                                # Beide vorhanden - deutsche Notation (1.234,56)
                                base_str = base_str.replace('.', '').replace(',', '.')
                            elif base_str.count('.') > 1:
                                # Mehrere Punkte - deutsche Tausender (1.234.567)
                                base_str = base_str.replace('.', '')
                            elif base_str.count(',') > 1:
                                # Mehrere Kommas - amerikanische Tausender (1,234,567)
                                base_str = base_str.replace(',', '')
                            elif '.' in base_str and len(base_str.split('.')[-1]) == 3 and not suffix:
                                # Punkt mit 3 Nachkommastellen ohne Suffix = deutsche Tausender
                                base_str = base_str.replace('.', '')
                            elif ',' in base_str and len(base_str.split(',')[-1]) == 3 and not suffix:
                                # Komma mit 3 Nachkommastellen ohne Suffix = amerikanische Tausender
                                base_str = base_str.replace(',', '')
                            else:
                                # Normalisiere zu Punkt als Dezimaltrennzeichen
                                base_str = base_str.replace(',', '.')
                            
                            base = float(base_str)
                            
                            # Anwenden von K/M Multipliers
                            if suffix and suffix.lower() == 'k':
                                base *= 1000
                            elif suffix and suffix.lower() == 'm':
                                base *= 1_000_000
                            
                            result = int(base)
                            
                            # Plausibilitätsprüfung
                            if 0 < result <= 50_000_000:  # Zwischen 1 Coin und 50M Coins
                                return result
                                
                        except (ValueError, OverflowError):
                            continue
        
        except Exception as e:
            logging.warning(f"Fehler beim Preis-Parsen von '{text}': {e}")
        
        return None

    def parse_alert_price(self, price_text: str) -> Optional[int]:
        """Parst Preis-Text für Alerts (z.B. 32k -> 32000, 1.5m -> 1500000)"""
        if not price_text:
            return None
        
        # Bereinige Input
        price_text = price_text.strip().replace(' ', '').lower()
        
        try:
            # Direkte Zahl ohne Suffix
            if price_text.isdigit():
                return int(price_text)
            
            # Mit k/m Suffix
            import re
            
            # Pattern für verschiedene Formate
            patterns = [
                r'(\d+(?:[.,]\d+)?)\s*k',    # 32k, 32.5k, 32,5k
                r'(\d+(?:[.,]\d+)?)\s*m',    # 1.5m, 1,5m
                r'(\d+(?:[.,]\d+)?)k',       # 32k (ohne Space)
                r'(\d+(?:[.,]\d+)?)m',       # 1.5m (ohne Space)
                r'(\d+(?:[.,]\d+)?)',        # Nur Zahl
            ]
            
            for pattern in patterns:
                match = re.search(pattern, price_text)
                if match:
                    base_str = match.group(1).replace(',', '.')
                    base = float(base_str)
                    
                    # Multiplier anwenden
                    if 'k' in price_text:
                        return int(base * 1000)
                    elif 'm' in price_text:
                        return int(base * 1000000)
                    else:
                        return int(base)
                        
        except (ValueError, AttributeError) as e:
            logging.warning(f"Konnte Preis nicht parsen: {price_text} - {e}")
            
        return None

    def _save_state(self):
        """Speichert den aktuellen Zustand ohne Backup"""
        try:
            # Speichere State direkt (ohne Backup)
            tmp_path = str(self.state_path) + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            
            # Atomisches Ersetzen
            Path(tmp_path).replace(self.state_path)
            
        except Exception as e:
            logging.error(f"Kritischer Fehler beim Speichern des States: {e}")
            # Fallback: Versuche direktes Schreiben
            try:
                with open(self.state_path, 'w', encoding='utf-8') as f:
                    json.dump(self.state, f, ensure_ascii=False, indent=2)
            except Exception as fallback_error:
                logging.error(f"Fallback-Speichern fehlgeschlagen: {fallback_error}")
                # State im Memory behalten, aber keine Persistierung möglich

    def price_changed(self, player: PlayerConfig, new_price: Optional[int]) -> bool:
        """Überprüft ob sich der Preis signifikant geändert hat"""
        if new_price is None:
            return False
        last = self.state.get(player.url, {}).get('price')
        if last is None:
            return True  # Erster Preis
        if last == 0:
            return True
        change_pct = abs(new_price - last) / last * 100
        threshold = player.threshold_percent or self.global_threshold
        return change_pct >= threshold

    def update_state(self, player: PlayerConfig, new_price: Optional[int], image_url: Optional[str]):
        """Aktualisiert den Zustand eines Spielers mit 24h-Tracking und Alert-Checking"""
        if new_price is None:
            return
        
        # Speichere alten Preis und 24h Daten
        old_data = self.state.get(player.url, {})
        old_price = old_data.get('price')
        current_time = int(time.time())
        
        # 24h-Preis verwalten
        price_24h_ago = old_data.get('price_24h_ago')
        price_24h_timestamp = old_data.get('price_24h_timestamp', 0)
        
        # Wenn mehr als 24h vergangen sind, aktualisiere 24h-Preis
        if current_time - price_24h_timestamp >= 86400:  # 86400 Sekunden = 24 Stunden
            if old_price is not None:
                price_24h_ago = old_price
            else:
                price_24h_ago = new_price
            price_24h_timestamp = current_time
        elif price_24h_ago is None:
            # Erstes Mal: Setze 24h-Preis auf aktuellen Preis
            price_24h_ago = new_price
            price_24h_timestamp = current_time
        
        # Alert-Checking: Prüfe ob Alerts ausgelöst werden sollen
        alert_triggered = self._check_price_alerts(player, old_price, new_price)
        
        self.state[player.url] = {
            'name': player.name,
            'price': new_price,
            'last_price': old_price,  # Für kurzfristige Trend-Berechnung
            'price_24h_ago': price_24h_ago,  # Preis vor 24h
            'price_24h_timestamp': price_24h_timestamp,  # Timestamp des 24h-Preises
            'image': image_url,
            'timestamp': current_time,
            'last_updated': datetime.now().isoformat(),
            'min_price': min(old_data.get('min_price', new_price), new_price),  # Niedrigster Preis
            'max_price': max(old_data.get('max_price', new_price), new_price),  # Höchster Preis
            'alert_triggered': alert_triggered  # Ob Alert ausgelöst wurde
        }

    def _check_price_alerts(self, player: PlayerConfig, old_price: Optional[int], new_price: int) -> bool:
        """Prüft ob Preis-Alerts ausgelöst werden sollen und sendet sie"""
        if not player.alert_user_id:
            return False
        
        alert_messages = []
        
        # Alert Above - Preis steigt über Schwellenwert
        if player.alert_above and new_price >= player.alert_above:
            if not old_price or old_price < player.alert_above:
                alert_messages.append({
                    'type': 'above',
                    'threshold': player.alert_above,
                    'price': new_price,
                    'direction': '📈'
                })
        
        # Alert Below - Preis fällt unter Schwellenwert
        if player.alert_below and new_price <= player.alert_below:
            if not old_price or old_price > player.alert_below:
                alert_messages.append({
                    'type': 'below',
                    'threshold': player.alert_below,
                    'price': new_price,
                    'direction': '📉'
                })
        
        # Sende Alerts
        if alert_messages:
            asyncio.create_task(self._send_price_alerts(player, alert_messages))
            return True
        
        return False

    async def _send_price_alerts(self, player: PlayerConfig, alerts: List[Dict[str, Any]]):
        """Sendet Preis-Alerts an den Benutzer"""
        if not self.dashboard_channel:
            return
        
        try:
            user = self.get_user(player.alert_user_id)
            user_mention = user.mention if user else f"<@{player.alert_user_id}>"
            
            for alert in alerts:
                embed = discord.Embed(
                    title=f"🚨 Preis-Alert: {player.name}",
                    color=COLOR_UP if alert['type'] == 'above' else COLOR_DOWN,
                    timestamp=datetime.now()
                )
                
                # Alert-Text
                if alert['type'] == 'above':
                    description = f"{alert['direction']} **Preis erreicht Ziel!**\n\n"
                    description += f"💰 **Aktueller Preis**: `{alert['price']:,}` Coins\n"
                    description += f"🎯 **Alert-Schwelle**: `{alert['threshold']:,}` Coins\n"
                    description += f"📊 **Status**: Preis ist **über** das Ziel gestiegen!"
                else:
                    description = f"{alert['direction']} **Preis unter Schwelle!**\n\n"
                    description += f"💰 **Aktueller Preis**: `{alert['price']:,}` Coins\n"
                    description += f"🎯 **Alert-Schwelle**: `{alert['threshold']:,}` Coins\n"
                    description += f"📊 **Status**: Preis ist **unter** die Schwelle gefallen!"
                
                embed.description = description
                
                # Spieler-Bild hinzufügen
                state_data = self.state.get(player.url, {})
                if state_data.get('image'):
                    embed.set_thumbnail(url=state_data['image'])
                
                embed.set_footer(text=f"Kategorie: {player.category} • FutBin Alert")
                
                # Sende mit User-Ping
                content = f"🔔 {user_mention} - Dein Alert für **{player.name}** wurde ausgelöst!"
                await self.dashboard_channel.send(content=content, embed=embed)
                
                logging.info(f"Alert gesendet für {player.name} an User {player.alert_user_id}")
                
        except Exception as e:
            logging.error(f"Fehler beim Senden der Preis-Alerts: {e}")

    @tasks.loop(seconds=PRICE_CHECK_INTERVAL)
    async def price_checker(self):
        """Background Task für Preis-Checks mit verbesserter Fehlerbehandlung"""
        if not self.players:
            return
        
        # Verhindere gleichzeitige Preis-Checks
        if price_check_semaphore.locked():
            logging.info("Preis-Check übersprungen - bereits in Bearbeitung")
            return
            
        async with price_check_semaphore:
            try:
                # Timeout für gesamten Preis-Check-Durchlauf (max 5 Minuten)
                await asyncio.wait_for(self._run_price_check(), timeout=300.0)
            except asyncio.TimeoutError:
                logging.error("Preis-Check Timeout erreicht (5 Minuten) - wird abgebrochen")
            except Exception as e:
                logging.error(f"Kritischer Fehler im Preis-Checker: {e}")
                await asyncio.sleep(30)  # Pause bei kritischen Fehlern
    
    async def _run_price_check(self):
        """Führt den eigentlichen Preis-Check durch"""
        start_time = time.time()
        logging.info(f"Starte Preis-Check für {len(self.players)} Spieler...")
        
        changes = []
        errors = []
        successful_checks = 0
        
        try:
            for i, player in enumerate(self.players):
                try:
                    # Rate Limiting: Pause zwischen Requests
                    if i > 0:
                        await asyncio.sleep(SLEEP_BETWEEN)                    # Timeout für einzelnen Player
                    try:
                        html = await asyncio.wait_for(
                            self.fetch_player_page(player.url), 
                            timeout=REQUEST_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        logging.warning(f"Timeout für {player.name} ({player.url})")
                        errors.append(f"Timeout: {player.name}")
                        continue
                    
                    if not html:
                        errors.append(f"Keine Daten: {player.name}")
                        continue
                    
                    # Parse mit Fehlerbehandlung
                    try:
                        parsed = self.parse_price_and_image(html)
                        new_price = parsed.get('price')
                        
                        if new_price is None:
                            logging.warning(f"Kein Preis gefunden für {player.name}")
                            errors.append(f"Kein Preis: {player.name}")
                            continue
                        
                        # Preis-Validierung (unrealistische Preise abfangen)
                        if new_price < 0 or new_price > 50_000_000:  # Max 50M Coins
                            logging.warning(f"Unrealistischer Preis für {player.name}: {new_price}")
                            errors.append(f"Unrealistischer Preis: {player.name}")
                            continue
                        
                        successful_checks += 1
                        
                        # Prüfe auf signifikante Änderungen
                        if self.price_changed(player, new_price):
                            old_price = self.state.get(player.url, {}).get('price')
                            changes.append({
                                'name': player.name,
                                'url': player.url,
                                'old_price': old_price,
                                'new_price': new_price,
                                'image': parsed.get('image')
                            })
                            logging.info(f"Preisänderung erkannt: {player.name} {old_price} -> {new_price}")
                        
                        # State aktualisieren (auch bei kleinen Änderungen)
                        self.update_state(player, new_price, parsed.get('image'))
                        
                    except Exception as parse_error:
                        logging.error(f"Parse-Fehler für {player.name}: {parse_error}")
                        errors.append(f"Parse-Fehler: {player.name}")
                        continue
                    
                except Exception as e:
                    logging.error(f"Unbekannter Fehler beim Prüfen von {player.name}: {e}")
                    errors.append(f"Fehler: {player.name}")
                    continue
            
            # Speichere State
            try:
                self._save_state()
            except Exception as e:
                logging.error(f"Fehler beim Speichern des States: {e}")
            
            # Performance-Logging
            duration = time.time() - start_time
            logging.info(f"Preis-Check abgeschlossen: {successful_checks}/{len(self.players)} erfolgreich in {duration:.1f}s")
            
            if errors:
                logging.warning(f"Fehler bei {len(errors)} Spielern: {', '.join(errors[:5])}")
            
            # Sende Änderungen mit Fehlerbehandlung
            if changes and self.dashboard_channel:
                try:
                    await self.send_price_changes(changes)
                except Exception as e:
                    logging.error(f"Fehler beim Senden der Preisänderungen: {e}")
            
            # Auto-Recovery: Wenn mehr als 50% Fehler, reduziere temporär die Check-Frequenz
            error_rate = len(errors) / len(self.players) if self.players else 0
            if error_rate > 0.5:
                logging.warning(f"Hohe Fehlerrate ({error_rate:.1%}), reduziere temporär Check-Frequenz")
                await asyncio.sleep(60)  # Extra 60s Pause
            
            # Update Task Health Monitoring
            self.last_price_check = time.time()
            self.task_errors["price_checker"] = len(errors)
            
        except Exception as e:
            logging.error(f"Kritischer Fehler in _run_price_check: {e}")
            self.task_errors["price_checker"] += 10  # Schwerwiegender Fehler

    async def send_price_changes(self, changes: List[Dict[str, Any]]):
        """Sendet Preisänderungen mit verbesserter Fehlerbehandlung"""
        if not self.dashboard_channel:
            return
        
        try:
            # Rate Limiting für Discord API
            if len(changes) > 10:
                # Bei vielen Änderungen: Sammle in einem Embed
                await self._send_bulk_price_changes(changes)
            else:
                # Bei wenigen Änderungen: Detaillierte Einzelmeldungen
                await self._send_individual_price_changes(changes)
                
        except discord.HTTPException as e:
            logging.error(f"Discord API Fehler beim Senden der Preisänderungen: {e}")
        except Exception as e:
            logging.error(f"Unerwarteter Fehler beim Senden der Preisänderungen: {e}")
    
    async def _send_bulk_price_changes(self, changes):
        """Sendet viele Änderungen in einem Sammel-Embed"""
        embed = discord.Embed(
            title=f"🔔 Preisänderungen ({len(changes)})",
            color=COLOR_BOT,
            timestamp=datetime.now()
        )
        
        description_lines = []
        for change in changes[:20]:  # Max 20 Einträge
            old_price = change['old_price']
            new_price = change['new_price']
            delta = new_price - (old_price if old_price else 0)
            
            if old_price:
                pct = (delta / old_price * 100)
                arrow = "📈" if delta > 0 else "📉"
                description_lines.append(
                    f"{arrow} **{change['name']}**: {new_price:,} ({delta:+,} | {pct:+.1f}%)"
                )
            else:
                description_lines.append(
                    f"🆕 **{change['name']}**: {new_price:,} Coins"
                )
        
        if len(changes) > 20:
            description_lines.append(f"... und {len(changes) - 20} weitere")
        
        embed.description = "\n".join(description_lines)
        await self.dashboard_channel.send(embed=embed)
    
    async def _send_individual_price_changes(self, changes):
        """Sendet wenige Änderungen als Einzelmeldungen"""
        for change in changes:
            embed = discord.Embed(
                title=f"💰 {change['name']}",
                url=change['url'],
                color=COLOR_UP if change['new_price'] > (change['old_price'] or 0) else COLOR_DOWN,
                timestamp=datetime.now()
            )
            
            old_price = change['old_price']
            new_price = change['new_price']
            
            if old_price:
                delta = new_price - old_price
                pct = (delta / old_price) * 100
                emoji = "📈" if delta > 0 else "📉"
                
                embed.description = (
                    f"{emoji} **Preis geändert**\n\n"
                    f"**Vorher**: `{old_price:,}` Coins\n"
                    f"**Nachher**: `{new_price:,}` Coins\n"
                    f"**Änderung**: {delta:+,} Coins ({pct:+.1f}%)"
                )
            else:
                embed.description = f"🆕 **Neuer Preis**: `{new_price:,}` Coins"
            
            if change.get('image'):
                embed.set_thumbnail(url=change['image'])
            
            await self.dashboard_channel.send(embed=embed)
            
            # Kleine Pause zwischen Nachrichten
            await asyncio.sleep(0.5)

    @tasks.loop(seconds=DASHBOARD_UPDATE_INTERVAL)
    async def dashboard_updater(self):
        """Background Task für Dashboard-Updates mit verbessertem Error-Handling"""
        try:
            if not self.dashboard_channel:
                logging.warning("Dashboard-Updater: Kein Dashboard Channel gesetzt")
                return
            
            # Timeout für Dashboard-Update
            await asyncio.wait_for(self.update_dashboard(), timeout=60.0)
            logging.debug("Dashboard-Update erfolgreich abgeschlossen")
            
            # Update Task Health Monitoring
            self.last_dashboard_update = time.time()
            self.task_errors["dashboard_updater"] = 0  # Reset bei Erfolg
            
            # Memory-Management für Discloud
            gc.collect()  # Garbage Collection nach Dashboard-Update
            
        except asyncio.TimeoutError:
            logging.error("Dashboard-Update Timeout erreicht (60s)")
            self.task_errors["dashboard_updater"] += 1
        except Exception as e:
            logging.error(f"Kritischer Fehler im Dashboard-Updater: {e}")
            self.task_errors["dashboard_updater"] += 1
            # Versuche den Bot zu stabilisieren, nicht abstürzen lassen
            try:
                await asyncio.sleep(10)  # Längere Pause vor Retry
                gc.collect()  # Memory freigeben
            except:
                pass

    async def create_dashboard_embed(self) -> discord.Embed:
        """Erstellt das Haupt-Dashboard Embed mit erweiterten Statistiken"""
        try:
            embed = discord.Embed(
                title="📊 FutBin Live Dashboard",
                color=COLOR_BOT,
                timestamp=datetime.now()
            )
            
            if not self.players:
                embed.description = (
                    "❌ Keine Spieler konfiguriert.\n"
                    "Verwende die Buttons unten oder `/add_player` um Spieler hinzuzufügen."
                )
                return embed
            
            # Sammle und analysiere Spielerdaten
            player_data = []
            total_value = 0
            total_min_value = 0
            total_max_value = 0
            available_count = 0
            price_changes_24h = []
            categories = {}
            
            for player in self.players:
                try:
                    state_data = self.state.get(player.url, {})
                    current_price = state_data.get('price')
                    price_24h_ago = state_data.get('price_24h_ago')
                    min_price = state_data.get('min_price', current_price if current_price else 0)
                    max_price = state_data.get('max_price', current_price if current_price else 0)
                    
                    # Kategorien sammeln
                    category = getattr(player, 'category', 'Allgemein')
                    if category not in categories:
                        categories[category] = {'count': 0, 'value': 0}
                    categories[category]['count'] += 1
                    
                    if current_price is None:
                        # Spieler ohne Preis
                        name = player.name[:15].ljust(15)
                        player_data.append({
                            'line': f"{name}    N/A        N/A    N/A",
                            'price': 0,
                            'volatility': 0,
                            'has_alert': bool(getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None))
                        })
                        continue
                    
                    available_count += 1
                    total_value += current_price
                    total_min_value += min_price
                    total_max_value += max_price
                    categories[category]['value'] += current_price
                    
                    # Spielername (maximal 13 Zeichen) + Alert-Indikator (immer 2 Zeichen reserviert)
                    alert_indicator = "🔔" if getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None) else "  "
                    player_name_short = player.name[:13].ljust(13)
                    name_with_alert = f"{alert_indicator}{player_name_short}"
                    
                    # Preis formatieren (in K/M)
                    if current_price >= 1_000_000:
                        price_str = f"{current_price/1_000_000:.1f}M".rjust(8)
                    elif current_price >= 1_000:
                        price_str = f"{current_price/1_000:.0f}K".rjust(8)
                    else:
                        price_str = f"{current_price}".rjust(8)
                    
                    # 24h-Änderung berechnen
                    if price_24h_ago and price_24h_ago > 0:
                        delta_24h = current_price - price_24h_ago
                        pct_24h = (delta_24h / price_24h_ago) * 100
                        price_changes_24h.append(pct_24h)
                        
                        # Formatiere 24h-Änderung
                        if abs(delta_24h) >= 1_000_000:
                            delta_str = f"{delta_24h/1_000_000:+.1f}M"
                        elif abs(delta_24h) >= 1_000:
                            delta_str = f"{delta_24h/1_000:+.0f}K"
                        else:
                            delta_str = f"{delta_24h:+.0f}"
                        
                        # Formatiere Prozent mit Farb-Emoji
                        if pct_24h > 0:
                            pct_str = f"📈{pct_24h:+.1f}%"
                        elif pct_24h < 0:
                            pct_str = f"📉{pct_24h:+.1f}%"
                        else:
                            pct_str = f"➖{pct_24h:+.1f}%"
                        
                        h24_str = f"{delta_str} {pct_str}".rjust(18)
                    else:
                        h24_str = "➖ +0.0%".rjust(18)
                        price_changes_24h.append(0)
                    
                    # Volatilität berechnen (Differenz zwischen Min/Max als Prozent)
                    volatility = 0
                    if min_price > 0 and max_price > min_price:
                        volatility = ((max_price - min_price) / min_price) * 100
                    
                    # Erstelle Zeile
                    line = f"{name_with_alert} {price_str} {h24_str}"
                    
                    player_data.append({
                        'line': line,
                        'price': current_price,
                        'volatility': volatility,
                        'has_alert': bool(getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None))
                    })
                        
                except Exception as e:
                    logging.error(f"Fehler beim Verarbeiten von Spieler {getattr(player, 'name', 'Unknown')}: {e}")
                    # Füge einen Fallback-Eintrag hinzu
                    player_data.append({
                        'line': f"{getattr(player, 'name', 'Error')[:15].ljust(15)}    ERROR      ERROR  ERROR",
                        'price': 0,
                        'volatility': 0,
                        'has_alert': False
                    })
                    continue
            
            # Sortiere nach Preis (höchster zuerst)
            player_data.sort(key=lambda x: x['price'], reverse=True)
            
            # Erstelle erweiterte Tabelle
            table_lines = []
            table_lines.append("```ansi")
            table_lines.append("Name             Preis              Trend      ")
            table_lines.append("─" * 56)
            
            # Füge Spielerzeilen zur Tabelle hinzu
            for data in player_data:
                table_lines.append(data['line'])
            
            # Erweiterte Statistiken
            table_lines.append("─" * 56)
            
            if available_count > 0:
                # Portfolio-Statistiken
                avg_value = total_value / available_count
                avg_volatility = sum(d['volatility'] for d in player_data) / len(player_data)
                portfolio_growth = sum(price_changes_24h) / len(price_changes_24h) if price_changes_24h else 0
                alert_count = sum(1 for d in player_data if d['has_alert'])
                
                # Portfolio-Wert Zeile
                if total_value >= 1_000_000:
                    total_str = f"Portfolio: {total_value/1_000_000:.1f}M"
                elif total_value >= 1_000:
                    total_str = f"Portfolio: {total_value/1_000:.0f}K"
                else:
                    total_str = f"Portfolio: {total_value:,.0f}"
                
                # Durchschnitt
                if avg_value >= 1_000_000:
                    avg_str = f"Ø {avg_value/1_000_000:.1f}M"
                elif avg_value >= 1_000:
                    avg_str = f"Ø {avg_value/1_000:.0f}K"
                else:
                    avg_str = f"Ø {avg_value:.0f}"
                
                # 24h Performance
                growth_emoji = "📈" if portfolio_growth > 0 else "📉" if portfolio_growth < 0 else "➖"
                performance_str = f"24h: {growth_emoji}{portfolio_growth:+.1f}%"
                
                table_lines.append(f"Spieler: {available_count}/{len(self.players):<8}")
                
            else:
                table_lines.append("Keine Preisdaten verfügbar")
            
            table_lines.append("```")
            
            embed.description = "\n".join(table_lines)
            
            # Zusätzliche Embed-Felder für erweiterte Infos
            if available_count > 0:
                # Top Performer (24h)
                top_performers = sorted(
                    [(d, p) for d, p in zip(player_data, self.players) if d['price'] > 0],
                    key=lambda x: self.state.get(x[1].url, {}).get('price_24h_ago', 0) and 
                                ((x[0]['price'] - self.state.get(x[1].url, {}).get('price_24h_ago', 0)) / 
                                 self.state.get(x[1].url, {}).get('price_24h_ago', 1)) * 100,
                    reverse=True
                )[:3]
                
                # Kategorien-Overview
                if len(categories) > 1:
                    cat_text = []
                    for cat, data in sorted(categories.items(), key=lambda x: x[1]['value'], reverse=True):
                        if data['value'] > 0:
                            if data['value'] >= 1_000_000:
                                value_str = f"{data['value']/1_000_000:.1f}M"
                            elif data['value'] >= 1_000:
                                value_str = f"{data['value']/1_000:.0f}K"
                            else:
                                value_str = f"{data['value']:,.0f}"
                            cat_text.append(f"📁 {cat}: {value_str} ({data['count']})")
                    
                    if cat_text:
                        embed.add_field(
                            name="📂 Portfolio nach Kategorien",
                            value="\n".join(cat_text[:5]),  # Max 5 Kategorien
                            inline=True
                        )
            
            embed.set_footer(text=f"PlayStation")
            
            return embed
            
        except Exception as e:
            logging.error(f"Kritischer Fehler beim Erstellen des Dashboard-Embeds: {e}")
            # Fallback Embed erstellen
            fallback_embed = discord.Embed(
                title="⚠️ Dashboard-Fehler",
                description=f"Ein Fehler ist beim Laden des Dashboards aufgetreten.\n\n**Fehler**: {str(e)[:100]}...",
                color=COLOR_DOWN,
                timestamp=datetime.now()
            )
            return fallback_embed

    async def update_dashboard(self):
        """Aktualisiert das Dashboard"""
        if not self.dashboard_channel:
            logging.warning("Dashboard Channel nicht gesetzt - überspringe Update")
            return
        
        embed = await self.create_dashboard_embed()
        view = DashboardView(self)
        
        try:
            # Wenn es bereits eine Dashboard-Nachricht gibt, editiere sie
            if self.dashboard_message_id:
                try:
                    message = await self.dashboard_channel.fetch_message(self.dashboard_message_id)
                    await message.edit(embed=embed, view=view)
                    logging.info("Dashboard erfolgreich aktualisiert")
                    return
                except discord.NotFound:
                    # Nachricht wurde gelöscht, erstelle neue
                    logging.warning("Dashboard-Nachricht nicht gefunden - erstelle neue")
                    self.dashboard_message_id = None
                except Exception as e:
                    logging.error(f"Fehler beim Editieren der Dashboard-Nachricht: {e}")
                    self.dashboard_message_id = None
            
            # Erstelle neue Dashboard-Nachricht
            message = await self.dashboard_channel.send(embed=embed, view=view)
            self.dashboard_message_id = message.id
            logging.info(f"Neues Dashboard erstellt - Message ID: {self.dashboard_message_id}")
            
            # Speichere die neue Message-ID in der Config
            await self.save_dashboard_message_id()
            
        except Exception as e:
            logging.error(f"Fehler beim Aktualisieren des Dashboards: {e}")

    async def save_dashboard_message_id(self):
        """Speichert die Dashboard-Message-ID in der Config"""
        try:
            # Config laden
            config_data = self.config.copy()
            if 'settings' not in config_data:
                config_data['settings'] = {}
            
            # Dashboard-Message-ID aktualisieren
            config_data['settings']['dashboard_message_id'] = self.dashboard_message_id
            
            # Config speichern
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)
                
            logging.info(f"Dashboard-Message-ID {self.dashboard_message_id} in Config gespeichert")
            
        except Exception as e:
            logging.error(f"Fehler beim Speichern der Dashboard-Message-ID: {e}")

    async def _create_detailed_portfolio_stats(self, players_list, category_filter=None):
        """Erstellt detaillierte Portfolio-Statistiken für gefilterte Spieler"""
        embed = discord.Embed(
            title=f"📊 Portfolio-Analyse" + (f" - {category_filter}" if category_filter else ""),
            color=COLOR_BOT,
            timestamp=datetime.now()
        )
        
        if not players_list:
            embed.description = "❌ Keine Spieler in diesem Filter"
            return embed
        
        # Sammle Statistiken
        total_value = 0
        total_min = 0
        total_max = 0
        available_count = 0
        price_changes = []
        alert_count = 0
        
        for player in players_list:
            state = self.state.get(player.url, {})
            price = state.get('price')
            if not price:
                continue
                
            available_count += 1
            total_value += price
            total_min += state.get('min_price', price)
            total_max += state.get('max_price', price)
            
            # Alerts zählen
            if getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None):
                alert_count += 1
            
            # 24h-Änderung
            price_24h = state.get('price_24h_ago', 0)
            if price_24h > 0:
                change = ((price - price_24h) / price_24h) * 100
                price_changes.append(change)
        
        if available_count == 0:
            embed.description = "❌ Keine Preisdaten verfügbar"
            return embed
        
        # Basis-Statistiken
        avg_value = total_value / available_count
        
        embed.add_field(
            name="💰 Portfolio-Wert",
            value=f"**Gesamt**: `{total_value:,}` Coins\n"
                    f"**Durchschnitt**: `{avg_value:,.0f}` Coins\n"
                    f"**Spieler**: {available_count}/{len(players_list)}",
            inline=True
        )
        
        # Performance
        if price_changes:
            avg_change = sum(price_changes) / len(price_changes)
            best_change = max(price_changes)
            worst_change = min(price_changes)
            
            emoji = "📈" if avg_change > 0 else "📉" if avg_change < 0 else "➖"
            
            embed.add_field(
                name="📊 24h Performance",
                value=f"**Portfolio**: {emoji} {avg_change:+.1f}%\n"
                      f"**Bester**: 📈 {best_change:+.1f}%\n"
                      f"**Schlechtester**: 📉 {worst_change:+.1f}%",
                inline=True
            )
        
        # Volatilität & Alerts
        if total_min > 0:
            volatility = ((total_max - total_min) / total_min) * 100
            
            embed.add_field(
                name="⚡ Analyse",
                value=f"**Volatilität**: {volatility:.1f}%\n"
                      f"**Alerts**: {alert_count}/{available_count}\n"
                      f"**Potential**: {((total_max - total_value) / total_value) * 100:+.1f}%",
                inline=True
            )
        
        # Top/Flop Performer
        performers = []
        for player in players_list:
            state = self.state.get(player.url, {})
            price = state.get('price')
            price_24h = state.get('price_24h_ago', 0)
            
            if price and price_24h > 0:
                change = ((price - price_24h) / price_24h) * 100
                performers.append((player.name, change, price))
        
        if performers:
            # Top 3 Performer
            top_performers = sorted(performers, key=lambda x: x[1], reverse=True)[:3]
            top_text = []
            for name, change, price in top_performers:
                emoji = "📈" if change > 0 else "📉" if change < 0 else "➖"
                top_text.append(f"{emoji} {name}: {change:+.1f}%")
            
            embed.add_field(
                name="🏆 Top Performer (24h)",
                value="\n".join(top_text),
                inline=True
            )
            
            # Wertvollste Spieler
            valuable = sorted(performers, key=lambda x: x[2], reverse=True)[:3]
            val_text = []
            for name, change, price in valuable:
                val_text.append(f"💎 {name}: `{price:,}` Coins")
            
            embed.add_field(
                name="💎 Wertvollste Spieler",
                value="\n".join(val_text),
                inline=True
            )
        
        embed.set_footer(text="Verwende /player_info <name> für Details zu einem Spieler")
        return embed

    async def _generate_simple_export(self):
        """Generiert einfachen Export-Text"""
        lines = []
        lines.append("=== FUTBIN PORTFOLIO EXPORT ===")
        lines.append(f"Datum: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        lines.append(f"Spieler: {len(self.players)}")
        lines.append("")
        
        total_value = 0
        for player in self.players:
            state = self.state.get(player.url, {})
            price = state.get('price', 0)
            total_value += price
            
            line = f"{player.name}: {price:,} Coins"
            if hasattr(player, 'category'):
                line += f" ({player.category})"
            lines.append(line)
        
        lines.append("")
        lines.append(f"Gesamtwert: {total_value:,} Coins")
        return "\n".join(lines)
    
    async def _generate_detailed_export(self):
        """Generiert detaillierten Export-Text"""
        lines = []
        lines.append("=== FUTBIN PORTFOLIO EXPORT (DETAILLIERT) ===")
        lines.append(f"Datum: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        lines.append(f"Spieler: {len(self.players)}")
        lines.append("")
        
        total_value = 0
        categories = {}
        
        for i, player in enumerate(self.players, 1):
            state = self.state.get(player.url, {})
            price = state.get('price', 0)
            min_price = state.get('min_price', price)
            max_price = state.get('max_price', price)
            price_24h = state.get('price_24h_ago', 0)
            
            total_value += price
            category = getattr(player, 'category', 'Allgemein')
            if category not in categories:
                categories[category] = []
            categories[category].append(price)
            
            lines.append(f"{i}. {player.name}")
            lines.append(f"   URL: {player.url}")
            lines.append(f"   Preis: {price:,} Coins")
            lines.append(f"   Min/Max: {min_price:,} - {max_price:,} Coins")
            
            if price_24h > 0:
                change = price - price_24h
                pct = (change / price_24h) * 100
                lines.append(f"   24h: {change:+,} ({pct:+.1f}%)")
            
            lines.append(f"   Kategorie: {category}")
            
            if hasattr(player, 'notes') and player.notes:
                lines.append(f"   Notizen: {player.notes}")
            
            # Alerts
            alerts = []
            if getattr(player, 'alert_above', None):
                alerts.append(f"Über {player.alert_above:,}")
            if getattr(player, 'alert_below', None):
                alerts.append(f"Unter {player.alert_below:,}")
            if alerts:
                lines.append(f"   Alerts: {', '.join(alerts)}")
            
            lines.append("")
        
        # Zusammenfassung
        lines.append("=== ZUSAMMENFASSUNG ===")
        lines.append(f"Gesamtwert: {total_value:,} Coins")
        if self.players:
            lines.append(f"Durchschnitt: {total_value//len(self.players):,} Coins")
        lines.append("")
        
        # Kategorien
        lines.append("=== KATEGORIEN ===")
        for cat, prices in sorted(categories.items()):
            cat_value = sum(prices)
            lines.append(f"{cat}: {cat_value:,} Coins ({len(prices)} Spieler)")
        
        return "\n".join(lines)

# Bot Instanz
bot = FutBinBot()

# Slash Commands (nach Bot-Instanziierung)
@bot.tree.command(name="setup", description="Komplettes Setup des FutBin Bots")
async def setup_bot(interaction: discord.Interaction):
    """Führt das komplette Bot-Setup durch"""
    await interaction.response.defer()
    
    embed = discord.Embed(
        title="🔧 FutBin Bot Setup",
        color=COLOR_BOT,
        timestamp=datetime.now()
    )
    
    setup_steps = []
    
    # 1. Dashboard Channel konfigurieren
    global DASHBOARD_CHANNEL_ID
    DASHBOARD_CHANNEL_ID = interaction.channel.id
    bot.dashboard_channel = interaction.channel
    
    if 'settings' not in bot.config:
        bot.config['settings'] = {}
    bot.config['settings']['dashboard_channel_id'] = DASHBOARD_CHANNEL_ID
    bot._save_config()
    
    setup_steps.append("✅ Dashboard Channel konfiguriert")
    
    # 2. Prüfe verfügbare Spieler
    player_count = len(bot.players)
    if player_count > 0:
        setup_steps.append(f"✅ {player_count} Spieler bereits konfiguriert")
    else:
        setup_steps.append("⚠️ Keine Spieler konfiguriert - verwende `/add_player`")
    
    # 3. Teste Preis-Updates für bestehende Spieler
    if bot.players:
        setup_steps.append("🔄 Teste Preis-Updates...")
        
        test_count = 0
        for player in bot.players[:3]:  # Teste nur die ersten 3 Spieler
            try:
                html = await bot.fetch_player_page(player.url)
                if html:
                    parsed = bot.parse_price_and_image(html)
                    if parsed.get('price'):
                        bot.update_state(player, parsed['price'], parsed.get('image'))
                        test_count += 1
            except Exception as e:
                logging.error(f"Fehler beim Testen von {player.name}: {e}")
        
        if test_count > 0:
            setup_steps.append(f"✅ {test_count} Spielerpreise erfolgreich getestet")
            bot._save_state()
        else:
            setup_steps.append("⚠️ Keine Preise erhalten - prüfe Internetverbindung")
    
    # 4. Erstelle Dashboard
    try:
        await bot.update_dashboard()
        setup_steps.append("✅ Dashboard erstellt")
    except Exception as e:
        setup_steps.append(f"❌ Dashboard-Fehler: {str(e)[:50]}")
    
    # 5. Background Tasks prüfen
    if bot.price_checker.is_running():
        setup_steps.append("✅ Preis-Checker läuft")
    else:
        setup_steps.append("⚠️ Preis-Checker nicht aktiv")
    
    if bot.dashboard_updater.is_running():
        setup_steps.append("✅ Dashboard-Updater läuft")
    else:
        setup_steps.append("⚠️ Dashboard-Updater nicht aktiv")
    
    embed.description = "\n".join(setup_steps)
    embed.add_field(
        name="📚 Nächste Schritte",
        value=(
            "• Verwende die **Buttons** im Dashboard um Spieler zu verwalten\n"
            "• Oder `/add_player <url>` für Command-basierte Verwaltung\n"
            "• Das Dashboard wird automatisch alle 5 Minuten aktualisiert\n"
            "• Preise werden alle 60 Sekunden überprüft\n"
            "• Beispiel URL: `https://www.futbin.com/26/player/234/viktor-gyokeres`"
        ),
        inline=False
    )
    
    await interaction.followup.send(embed=embed)

# Slash Commands (nach Bot-Instanziierung)
@bot.tree.command(name="set_dashboard", description="Setzt den aktuellen Channel als Dashboard Channel")
async def set_dashboard(interaction: discord.Interaction):
    """Setzt den Dashboard Channel"""
    global DASHBOARD_CHANNEL_ID
    
    DASHBOARD_CHANNEL_ID = interaction.channel.id
    bot.dashboard_channel = interaction.channel
    
    # Speichere in Konfiguration
    if 'settings' not in bot.config:
        bot.config['settings'] = {}
    bot.config['settings']['dashboard_channel_id'] = DASHBOARD_CHANNEL_ID
    bot._save_config()
    
    embed = discord.Embed(
        title="✅ Dashboard konfiguriert",
        description=f"Dieser Channel wurde als Dashboard Channel gesetzt.\nDas Live-Dashboard wird hier alle {DASHBOARD_UPDATE_INTERVAL//60} Minuten aktualisiert.",
        color=COLOR_BOT
    )
    
    await interaction.response.send_message(embed=embed)
    
    # Erstelle sofort ein Dashboard
    await bot.update_dashboard()

@bot.tree.command(name="dashboard", description="Erstellt ein neues Dashboard (löscht das alte)")
async def refresh_dashboard(interaction: discord.Interaction):
    """Erstellt ein neues Dashboard und löscht das alte"""
    
    # Prüfe, ob die Interaktion bereits beantwortet wurde
    if interaction.response.is_done():
        return
        
    await interaction.response.defer()
    
    try:
        # Dashboard Channel finden
        dashboard_channel = bot.get_channel(bot.config.get('settings', {}).get('dashboard_channel_id', DASHBOARD_CHANNEL_ID))
        if not dashboard_channel:
            await interaction.followup.send("❌ Dashboard Channel nicht konfiguriert! Verwende `/set_dashboard` zuerst.", ephemeral=True)
            return
        
        # Setze den Dashboard Channel falls noch nicht gesetzt
        if not bot.dashboard_channel:
            bot.dashboard_channel = dashboard_channel
        
        # Lösche die alte Dashboard-Nachricht falls vorhanden
        if bot.dashboard_message_id:
            try:
                old_message = await dashboard_channel.fetch_message(bot.dashboard_message_id)
                await old_message.delete()
                logging.info(f"Alte Dashboard-Nachricht gelöscht - Message ID: {bot.dashboard_message_id}")
            except discord.NotFound:
                logging.info("Alte Dashboard-Nachricht bereits gelöscht oder nicht gefunden")
            except Exception as e:
                logging.warning(f"Konnte alte Dashboard-Nachricht nicht löschen: {e}")
        
        # Erstelle IMMER eine neue Dashboard-Nachricht
        embed = await bot.create_dashboard_embed()
        view = DashboardView(bot)
        
        # Dashboard senden (neue Nachricht)
        message = await dashboard_channel.send(embed=embed, view=view)
        
        # Setze die neue Message-ID als aktive Dashboard-Message
        bot.dashboard_message_id = message.id
        await bot.save_dashboard_message_id()
        
        logging.info(f"Neues Dashboard erstellt via Command - Message ID: {message.id}")
        
        # Temporäre Bestätigung (wird nach 5 Sekunden gelöscht)
        temp_msg = await interaction.followup.send(f"✅ Neues Dashboard erstellt! Das alte wurde entfernt.", ephemeral=True)
        
    except Exception as e:
        logging.error(f"Fehler beim Erstellen eines neuen Dashboards: {e}")
        await interaction.followup.send(f"❌ Fehler beim Erstellen des Dashboards: {str(e)}", ephemeral=True)

@bot.tree.command(name="add_player", description="Fügt einen Spieler zur Überwachung hinzu")
async def add_player(interaction: discord.Interaction, futbin_url: str):
    """Fügt einen neuen Spieler hinzu (automatische Namens-Erkennung)"""
    
    # Validiere URL
    if not futbin_url.startswith("https://www.futbin.com/"):
        embed = discord.Embed(
            title="❌ Ungültige URL",
            description="Die URL muss mit `https://www.futbin.com/` beginnen.\n\nBeispiel: `https://www.futbin.com/26/player/234/viktor-gyokeres`",
            color=COLOR_DOWN
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Prüfe ob Spieler bereits existiert
    for player in bot.players:
        if player.url == futbin_url:
            embed = discord.Embed(
                title="❌ Spieler bereits vorhanden",
                description=f"**{player.name}** wird bereits überwacht.",
                color=COLOR_DOWN
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Teste die URL
    await interaction.response.defer()
    
    try:
        html = await bot.fetch_player_page(futbin_url)
        if not html:
            embed = discord.Embed(
                title="❌ URL nicht erreichbar",
                description="Die angegebene URL konnte nicht erreicht werden.",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Parse Spielerdaten mit automatischer Namens-Erkennung
        parsed = bot.parse_price_and_image(html)
        player_name = parsed.get('name')
        price = parsed.get('price')
        
        if not player_name:
            # Fallback: Name aus URL extrahieren
            import re
            match = re.search(r'/([^/]+)/?$', futbin_url.rstrip('/'))
            if match:
                player_name = match.group(1).replace('-', ' ').title()
            else:
                player_name = "Unbekannter Spieler"
        
        if price is None:
            embed = discord.Embed(
                title="⚠️ Warnung",
                description=f"**{player_name}** wurde hinzugefügt, aber kein Preis gefunden. Der Spieler wird trotzdem überwacht.",
                color=COLOR_NEUTRAL
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
    
    except Exception as e:
        embed = discord.Embed(
            title="❌ Fehler beim Testen der URL",
            description=f"Fehler: {str(e)}",
            color=COLOR_DOWN
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Füge Spieler hinzu
    new_player = PlayerConfig(name=player_name, url=futbin_url, active=True)
    bot.players.append(new_player)
    
    # Aktualisiere Konfiguration
    if 'players' not in bot.config:
        bot.config['players'] = []
    
    bot.config['players'].append({
        'name': player_name,
        'url': futbin_url,
        'active': True
    })
    
    bot._save_config()
    
    # Erstelle initialen State-Eintrag
    if price:
        bot.update_state(new_player, price, parsed.get('image'))
        bot._save_state()
    
    embed = discord.Embed(
        title="✅ Spieler hinzugefügt",
        description=f"**{player_name}** wird jetzt überwacht!\n{f'Aktueller Preis: `{price:,}` Coins' if price else 'Preis wird beim nächsten Update geholt'}",
        color=COLOR_UP
    )
    
    
    if parsed.get('image'):
        embed.set_thumbnail(url=parsed['image'])
    
    await interaction.followup.send(embed=embed)
    
    # Aktualisiere Dashboard
    if bot.dashboard_channel:
        await bot.update_dashboard()

@bot.tree.command(name="remove_player", description="Entfernt einen Spieler aus der Überwachung")
async def remove_player(interaction: discord.Interaction, name: str):
    """Entfernt einen Spieler"""
    
    # Suche Spieler
    player_to_remove = None
    for player in bot.players:
        if player.name.lower() == name.lower():
            player_to_remove = player
            break
    
    if not player_to_remove:
        embed = discord.Embed(
            title="❌ Spieler nicht gefunden",
            description=f"**{name}** wird nicht überwacht.\nVerwende `/list_players` um alle Spieler zu sehen.",
            color=COLOR_DOWN
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Entferne aus der Liste
    bot.players.remove(player_to_remove)
    
    # Entferne aus Konfiguration
    bot.config['players'] = [
        p for p in bot.config.get('players', []) 
        if p.get('name', '').lower() != name.lower()
    ]
    bot._save_config()
    
    # Entferne aus State (optional)
    if player_to_remove.url in bot.state:
        del bot.state[player_to_remove.url]
        bot._save_state()
    
    embed = discord.Embed(
        title="✅ Spieler entfernt",
        description=f"**{player_to_remove.name}** wird nicht mehr überwacht.",
        color=COLOR_UP
    )
    
    await interaction.response.send_message(embed=embed)
    
    # Aktualisiere Dashboard
    if bot.dashboard_channel:
        await bot.update_dashboard()

@bot.tree.command(name="list_players", description="Zeigt alle überwachten Spieler an")
async def list_players(interaction: discord.Interaction):
    """Listet alle Spieler auf"""
    
    if not bot.players:
        embed = discord.Embed(
            title="📋 Spielerliste",
            description="❌ Keine Spieler konfiguriert.\nVerwende `/add_player` um Spieler hinzuzufügen.",
            color=COLOR_NEUTRAL
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = discord.Embed(
        title=f"📋 Überwachte Spieler ({len(bot.players)})",
        color=COLOR_BOT
    )
    
    description_lines = []
    for i, player in enumerate(bot.players, 1):
        state_data = bot.state.get(player.url, {})
        price = state_data.get('price')
        price_text = f"`{price:,}` Coins".replace(',', '.') if price else "`Unbekannt`"
        description_lines.append(f"{i}. **{player.name}** - {price_text}")
    
    embed.description = "\n".join(description_lines)
    embed.set_footer(text="Verwende /remove_player <name> um einen Spieler zu entfernen")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="force_update", description="Erzwingt ein sofortiges Dashboard-Update")
async def force_update(interaction: discord.Interaction):
    """Erzwingt ein Dashboard-Update"""
    await interaction.response.defer()
    
    if not bot.dashboard_channel:
        embed = discord.Embed(
            title="❌ Kein Dashboard konfiguriert",
            description="Verwende `/dashboard` um einen Dashboard Channel zu setzen.",
            color=COLOR_DOWN
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Führe Preis-Update durch
    await bot.price_checker()
    
    # Aktualisiere Dashboard
    await bot.update_dashboard()
    
    embed = discord.Embed(
        title="✅ Update abgeschlossen",
        description="Dashboard wurde aktualisiert!",
        color=COLOR_UP
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="info", description="Zeigt Bot-Informationen und Status an")
async def bot_info(interaction: discord.Interaction):
    """Zeigt Bot-Informationen"""
    embed = discord.Embed(
        title="ℹ️ FutBin Bot Information",
        color=COLOR_BOT,
        timestamp=datetime.now()
    )
    
    # Status-Informationen
    status_lines = []
    status_lines.append(f"🤖 **Bot Status**: Online")
    status_lines.append(f"📊 **Dashboard Channel**: {f'<#{DASHBOARD_CHANNEL_ID}>' if DASHBOARD_CHANNEL_ID else 'Nicht konfiguriert'}")
    status_lines.append(f"👥 **Überwachte Spieler**: {len(bot.players)}")
    status_lines.append(f"🔄 **Preis-Checker**: {'🟢 Aktiv' if bot.price_checker.is_running() else '🔴 Inaktiv'}")
    status_lines.append(f"📈 **Dashboard-Updater**: {'🟢 Aktiv' if bot.dashboard_updater.is_running() else '🔴 Inaktiv'}")
    
    embed.add_field(
        name="📊 Status",
        value="\n".join(status_lines),
        inline=False
    )
    
    # Konfiguration
    config_lines = []
    config_lines.append(f"🎮 **Platform**: {bot.config.get('settings', {}).get('platform', 'ps').upper()}")
    config_lines.append(f"📊 **Preisschwelle**: {bot.global_threshold}%")
    config_lines.append(f"⏱️ **Update-Intervall**: {DASHBOARD_UPDATE_INTERVAL//60} Minuten")
    config_lines.append(f"🔍 **Preis-Check**: {PRICE_CHECK_INTERVAL} Sekunden")
    
    embed.add_field(
        name="⚙️ Konfiguration",
        value="\n".join(config_lines),
        inline=False
    )
    
    # Commands
    commands_info = (
        "`/setup` - Komplettes Bot-Setup\n"
        "`/dashboard` - Dashboard Channel setzen\n"
        "`/add_player <url>` - Spieler hinzufügen (automatischer Name)\n"
        "`/remove_player` - Spieler entfernen\n"
        "`/list_players` - Alle Spieler anzeigen\n"
        "`/force_update` - Sofortiges Update\n"
        "`/debug` - Debug-Informationen\n"
        "**Dashboard-Buttons** - Interaktive Verwaltung"
    )
    
    embed.add_field(
        name="🔧 Verfügbare Commands",
        value=commands_info,
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="test", description="Testet Bot-Funktionalität und Permissions")
async def test_bot(interaction: discord.Interaction):
    """Testet Bot-Funktionalität"""
    embed = discord.Embed(
        title="🧪 Bot-Test",
        description="Bot läuft und kann Slash Commands empfangen!",
        color=COLOR_BOT,
        timestamp=datetime.now()
    )
    
    # Bot-Informationen
    bot_info = []
    bot_info.append(f"**Bot Name**: {bot.user.name}")
    bot_info.append(f"**Bot ID**: {bot.user.id}")
    bot_info.append(f"**Server**: {interaction.guild.name}")
    bot_info.append(f"**Channel**: {interaction.channel.name}")
    
    embed.add_field(
        name="🤖 Bot-Info",
        value="\n".join(bot_info),
        inline=False
    )
    
    # Permissions testen
    permissions = interaction.channel.permissions_for(interaction.guild.me)
    perm_info = []
    perm_info.append(f"Send Messages: {'✅' if permissions.send_messages else '❌'}")
    perm_info.append(f"Embed Links: {'✅' if permissions.embed_links else '❌'}")
    perm_info.append(f"Use Slash Commands: {'✅' if permissions.use_slash_commands else '❌'}")
    perm_info.append(f"Manage Messages: {'✅' if permissions.manage_messages else '❌'}")
    
    embed.add_field(
        name="🔐 Permissions",
        value="\n".join(perm_info),
        inline=False
    )
    
    # Status-Info
    status_info = []
    status_info.append(f"Dashboard Channel: {'✅ Gesetzt' if bot.dashboard_channel else '❌ Nicht gesetzt'}")
    status_info.append(f"Spieler geladen: {len(bot.players)}")
    status_info.append(f"Preis-Checker: {'🟢 Läuft' if bot.price_checker.is_running() else '🔴 Gestoppt'}")
    
    embed.add_field(
        name="📊 Status",
        value="\n".join(status_info),
        inline=False
    )
    
    try:
        await interaction.response.send_message(embed=embed)
        logging.info("✅ Test-Command erfolgreich ausgeführt")
    except Exception as e:
        logging.error(f"❌ Fehler beim Test-Command: {e}")
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)

@bot.tree.command(name="set_alert", description="Setzt einen Preis-Alert für einen Spieler")
async def set_alert(interaction: discord.Interaction, player_name: str, price: str, alert_type: str = "über"):
    """Setzt einen Preis-Alert für einen Spieler (z.B. /set_alert Kane 50k über)"""
    await interaction.response.defer()
    
    try:
        # Parse Preis-Input (32k -> 32000, 1.5m -> 1500000)
        parsed_price = bot.parse_alert_price(price)
        if parsed_price is None or parsed_price <= 0:
            embed = discord.Embed(
                title="❌ Ungültiger Preis",
                description=f"**{price}** ist kein gültiger Preis.\n\n**Beispiele:**\n• `50k` = 50.000 Coins\n• `1.5m` = 1.500.000 Coins\n• `250000` = 250.000 Coins",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Parse Alert-Typ
        alert_type_lower = alert_type.lower().strip()
        is_above_alert = True  # Default
        
        if alert_type_lower in ['unter', 'below', 'less', '<', 'kleiner']:
            is_above_alert = False
        elif alert_type_lower in ['über', 'ueber', 'above', 'more', '>', 'größer', 'groesser']:
            is_above_alert = True
        else:
            embed = discord.Embed(
                title="❌ Ungültiger Alert-Typ",
                description=f"**{alert_type}** ist kein gültiger Alert-Typ.\n\n**Gültige Werte:**\n• `über` - benachrichtigen wenn Preis erreicht/überschritten wird\n• `unter` - benachrichtigen wenn Preis unterschritten wird",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Finde Spieler
        player_found = None
        for player in bot.players:
            if player.name.lower() == player_name.lower():
                player_found = player
                break
        
        if not player_found:
            embed = discord.Embed(
                title="❌ Spieler nicht gefunden",
                description=f"**{player_name}** wird nicht überwacht.\n\nVerwende `/list_players` um alle Spieler zu sehen.",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Setze Alert basierend auf Typ
        if is_above_alert:
            player_found.alert_above = parsed_price
            player_found.alert_below = None
            alert_description = f"Du wirst benachrichtigt wenn **{player_found.name}** {parsed_price:,} Coins erreicht oder überschreitet."
        else:
            player_found.alert_below = parsed_price
            player_found.alert_above = None
            alert_description = f"Du wirst benachrichtigt wenn **{player_found.name}** unter {parsed_price:,} Coins fällt."
        player_found.alert_user_id = interaction.user.id
        
        # Aktualisiere Konfiguration
        for config_player in bot.config.get('players', []):
            if config_player.get('url') == player_found.url:
                if is_above_alert:
                    config_player['alert_above'] = parsed_price
                    config_player['alert_below'] = None
                else:
                    config_player['alert_below'] = parsed_price  
                    config_player['alert_above'] = None
                config_player['alert_user_id'] = interaction.user.id
                break
        
        bot._save_config()
        
        # Erfolgsmeldung
        current_price = bot.state.get(player_found.url, {}).get('price', 0)
        
        if is_above_alert:
            status_emoji = "🟢" if isinstance(current_price, int) and current_price >= parsed_price else "🔴"
            alert_info = f"📈 **Alert bei**: Über {parsed_price:,} Coins"
        else:
            status_emoji = "🟢" if isinstance(current_price, int) and current_price <= parsed_price else "🔴"
            alert_info = f"📉 **Alert bei**: Unter {parsed_price:,} Coins"
        
        embed = discord.Embed(
            title="✅ Alert gesetzt!",
            description=alert_description,
            color=COLOR_UP
        )
        
        embed.add_field(
            name="📊 Status",
            value=f"{status_emoji} **Aktuell**: {current_price:,} Coins\n{alert_info}",
            inline=False
        )
        
        embed.add_field(
            name="👤 Alert-Inhaber",
            value=f"{interaction.user.mention} ({interaction.user.display_name})",
            inline=False
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        # Aktualisiere Dashboard
        await bot.update_dashboard()
        
    except Exception as e:
        logging.error(f"Fehler beim Setzen des Alerts: {e}")
        embed = discord.Embed(
            title="❌ Fehler beim Alert setzen",
            description=f"Ein unerwarteter Fehler ist aufgetreten: {str(e)}",
            color=COLOR_DOWN
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="list_alerts", description="Zeigt alle deine gesetzten Alerts an")
async def list_alerts(interaction: discord.Interaction):
    """Listet alle Alerts des Users auf"""
    
    user_alerts = []
    for player in bot.players:
        if player.alert_user_id == interaction.user.id:
            user_alerts.append(player)
    
    if not user_alerts:
        embed = discord.Embed(
            title="📋 Deine Alerts",
            description="❌ Du hast noch keine Alerts gesetzt.\nVerwende `/set_alert` um einen Alert zu erstellen.",
            color=COLOR_NEUTRAL
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"📋 Deine Alerts ({len(user_alerts)})",
        color=COLOR_BOT,
        timestamp=datetime.now()
    )
    
    for player in user_alerts:
        current_state = bot.state.get(player.url, {})
        current_price = current_state.get('price', 'Unbekannt')
        
        alert_info = []
        if player.alert_above:
            status = "🟢" if isinstance(current_price, int) and current_price >= player.alert_above else "🔴"
            alert_info.append(f"{status} Über: `{player.alert_above:,}` Coins")
        
        if player.alert_below:
            status = "🟢" if isinstance(current_price, int) and current_price <= player.alert_below else "🔴"
            alert_info.append(f"{status} Unter: `{player.alert_below:,}` Coins")
        
        field_value = (
            f"**Aktuell**: `{current_price:,}` Coins\n" if isinstance(current_price, int) else "**Aktuell**: Unbekannt\n"
        ) + "\n".join(alert_info) + f"\n**Kategorie**: {player.category}"
        
        embed.add_field(
            name=f"🎯 {player.name}",
            value=field_value,
            inline=True
        )
    
    embed.set_footer(text="🟢 = Alert bereit • 🔴 = Alert bereits ausgelöst • Verwende /remove_alert zum Löschen")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove_alert", description="Entfernt einen Alert von einem Spieler")
async def remove_alert(interaction: discord.Interaction, player_name: str):
    """Entfernt Alerts für einen Spieler"""
    
    # Finde Spieler
    player_found = None
    for player in bot.players:
        if player.name.lower() == player_name.lower() and player.alert_user_id == interaction.user.id:
            player_found = player
            break
    
    if not player_found:
        embed = discord.Embed(
            title="❌ Alert nicht gefunden",
            description=f"Du hast keinen Alert für **{player_name}** gesetzt.",
            color=COLOR_DOWN
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Entferne Alerts
    old_alerts = []
    if player_found.alert_above:
        old_alerts.append(f"Über: `{player_found.alert_above:,}` Coins")
    if player_found.alert_below:
        old_alerts.append(f"Unter: `{player_found.alert_below:,}` Coins")
    
    player_found.alert_above = None
    player_found.alert_below = None
    player_found.alert_user_id = None
    
    # Aktualisiere Konfiguration
    for config_player in bot.config.get('players', []):
        if config_player.get('url') == player_found.url:
            config_player.pop('alert_above', None)
            config_player.pop('alert_below', None)
            config_player.pop('alert_user_id', None)
            break
    
    bot._save_config()
    
    embed = discord.Embed(
        title="✅ Alert entfernt",
        description=f"**{player_found.name}**\n\nEntfernte Alerts:\n" + "\n".join(old_alerts),
        color=COLOR_UP
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="portfolio", description="Zeigt detaillierte Portfolio-Statistiken")
async def portfolio_stats(interaction: discord.Interaction, category: str = None):
    """Zeigt Portfolio-Statistiken, optional gefiltert nach Kategorie"""
    await interaction.response.defer()
    
    # Filtere Spieler nach Kategorie falls angegeben
    filtered_players = bot.players
    if category:
        filtered_players = [p for p in bot.players if getattr(p, 'category', 'Allgemein').lower() == category.lower()]
        if not filtered_players:
            embed = discord.Embed(
                title="❌ Kategorie nicht gefunden",
                description=f"Keine Spieler in Kategorie **{category}** gefunden.",
                color=COLOR_DOWN
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
    
    embed = await bot._create_detailed_portfolio_stats(filtered_players, category)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="categories", description="Zeigt alle verfügbaren Kategorien")
async def list_categories(interaction: discord.Interaction):
    """Listet alle Kategorien auf"""
    categories = {}
    for player in bot.players:
        cat = getattr(player, 'category', 'Allgemein')
        if cat not in categories:
            categories[cat] = {'count': 0, 'value': 0}
        categories[cat]['count'] += 1
        
        # Hole Preis
        state = bot.state.get(player.url, {})
        price = state.get('price', 0)
        categories[cat]['value'] += price
    
    if not categories:
        embed = discord.Embed(
            title="📂 Kategorien",
            description="❌ Keine Kategorien gefunden.",
            color=COLOR_NEUTRAL
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"📂 Portfolio-Kategorien ({len(categories)})",
        color=COLOR_BOT,
        timestamp=datetime.now()
    )
    
    # Sortiere nach Wert
    sorted_cats = sorted(categories.items(), key=lambda x: x[1]['value'], reverse=True)
    
    for cat, data in sorted_cats:
        if data['value'] >= 1_000_000:
            value_str = f"{data['value']/1_000_000:.1f}M Coins"
        elif data['value'] >= 1_000:
            value_str = f"{data['value']/1_000:.0f}K Coins"
        else:
            value_str = f"{data['value']:,} Coins"
        
        embed.add_field(
            name=f"📁 {cat}",
            value=f"**Spieler**: {data['count']}\n**Wert**: {value_str}",
            inline=True
        )
    
    embed.set_footer(text="Verwende /portfolio <kategorie> für Details")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_category", description="Setzt die Kategorie für einen Spieler")
async def set_category(interaction: discord.Interaction, player_name: str, category: str):
    """Setzt eine Kategorie für einen Spieler"""
    
    # Finde Spieler
    player_found = None
    for player in bot.players:
        if player.name.lower() == player_name.lower():
            player_found = player
            break
    
    if not player_found:
        embed = discord.Embed(
            title="❌ Spieler nicht gefunden",
            description=f"**{player_name}** wird nicht überwacht.",
            color=COLOR_DOWN
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Setze Kategorie
    old_category = getattr(player_found, 'category', 'Allgemein')
    player_found.category = category
    
    # Aktualisiere Konfiguration
    for config_player in bot.config.get('players', []):
        if config_player.get('url') == player_found.url:
            config_player['category'] = category
            break
    
    bot._save_config()
    
    embed = discord.Embed(
        title="✅ Kategorie aktualisiert",
        description=f"**{player_found.name}**\n\n"
                   f"**Alte Kategorie**: {old_category}\n"
                   f"**Neue Kategorie**: {category}",
        color=COLOR_UP
    )
    
    # Zeige aktuellen Preis
    state = bot.state.get(player_found.url, {})
    if state.get('price'):
        embed.add_field(
            name="💰 Aktueller Preis",
            value=f"`{state['price']:,}` Coins",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # Aktualisiere Dashboard
    if bot.dashboard_channel:
        await bot.update_dashboard()

@bot.tree.command(name="add_note", description="Fügt eine Notiz zu einem Spieler hinzu")
async def add_note(interaction: discord.Interaction, player_name: str, note: str):
    """Fügt eine Notiz zu einem Spieler hinzu"""
    
    # Finde Spieler
    player_found = None
    for player in bot.players:
        if player.name.lower() == player_name.lower():
            player_found = player
            break
    
    if not player_found:
        embed = discord.Embed(
            title="❌ Spieler nicht gefunden",
            description=f"**{player_name}** wird nicht überwacht.",
            color=COLOR_DOWN
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Setze Notiz
    player_found.notes = note
    
    # Aktualisiere Konfiguration
    for config_player in bot.config.get('players', []):
        if config_player.get('url') == player_found.url:
            config_player['notes'] = note
            break
    
    bot._save_config()
    
    embed = discord.Embed(
        title="✅ Notiz hinzugefügt",
        description=f"**{player_found.name}**\n\n📝 **Notiz**: {note}",
        color=COLOR_UP
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="player_info", description="Zeigt detaillierte Informationen zu einem Spieler")
async def player_info(interaction: discord.Interaction, player_name: str):
    """Zeigt detaillierte Spieler-Informationen"""
    
    # Finde Spieler
    player_found = None
    for player in bot.players:
        if player.name.lower() == player_name.lower():
            player_found = player
            break
    
    if not player_found:
        embed = discord.Embed(
            title="❌ Spieler nicht gefunden",
            description=f"**{player_name}** wird nicht überwacht.",
            color=COLOR_DOWN
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Hole State-Daten
    state = bot.state.get(player_found.url, {})
    
    embed = discord.Embed(
        title=f"📊 {player_found.name}",
        url=player_found.url,
        color=COLOR_BOT,
        timestamp=datetime.now()
    )
    
    # Preis-Informationen
    current_price = state.get('price')
    if current_price:
        min_price = state.get('min_price', current_price)
        max_price = state.get('max_price', current_price)
        price_24h = state.get('price_24h_ago', 0)
        
        price_info = f"**Aktuell**: `{current_price:,}` Coins\n"
        price_info += f"**Minimum**: `{min_price:,}` Coins\n"
        price_info += f"**Maximum**: `{max_price:,}` Coins\n"
        
        if price_24h > 0:
            change_24h = current_price - price_24h
            pct_24h = (change_24h / price_24h) * 100
            emoji = "📈" if change_24h > 0 else "📉" if change_24h < 0 else "➖"
            price_info += f"**24h**: {emoji} {change_24h:+,} ({pct_24h:+.1f}%)"
        
        embed.add_field(
            name="💰 Preis-Daten",
            value=price_info,
            inline=False
        )
        
        # Volatilität
        if min_price > 0:
            volatility = ((max_price - min_price) / min_price) * 100
            position = ((current_price - min_price) / (max_price - min_price)) * 100 if max_price > min_price else 50
            
            embed.add_field(
                name="⚡ Volatilität",
                value=f"**Spread**: {volatility:.1f}%\n**Position**: {position:.0f}% vom Hoch",
                inline=True
            )
    else:
        embed.add_field(
            name="💰 Preis-Daten",
            value="❌ Keine Preisdaten verfügbar",
            inline=False
        )
    
    # Kategorie und Notizen
    meta_info = f"**Kategorie**: {getattr(player_found, 'category', 'Allgemein')}\n"
    
    if hasattr(player_found, 'notes') and player_found.notes:
        meta_info += f"**Notizen**: {player_found.notes}"
    else:
        meta_info += "**Notizen**: Keine"
    
    embed.add_field(
        name="📁 Meta-Daten",
        value=meta_info,
        inline=True
    )
    
    # Alerts
    alerts_info = []
    if getattr(player_found, 'alert_above', None):
        alerts_info.append(f"📈 Über: `{player_found.alert_above:,}` Coins")
    if getattr(player_found, 'alert_below', None):
        alerts_info.append(f"📉 Unter: `{player_found.alert_below:,}` Coins")
    
    if alerts_info:
        alerts_user = getattr(player_found, 'alert_user_id', None)
        user_mention = f"<@{alerts_user}>" if alerts_user else "Unbekannt"
        embed.add_field(
            name="🚨 Aktive Alerts",
            value="\n".join(alerts_info) + f"\n**User**: {user_mention}",
            inline=True
        )
    
    # Spieler-Bild
    if state.get('image'):
        embed.set_thumbnail(url=state['image'])
    
    # Zeitstempel
    last_updated = state.get('last_updated')
    if last_updated:
        embed.set_footer(text=f"Letztes Update: {last_updated[:19].replace('T', ' ')}")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="export_portfolio", description="Exportiert dein Portfolio als Text")
async def export_portfolio(interaction: discord.Interaction, format: str = "simple"):
    """Exportiert Portfolio-Daten"""
    await interaction.response.defer(ephemeral=True)
    
    if not bot.players:
        embed = discord.Embed(
            title="❌ Leeres Portfolio",
            description="Keine Spieler zum Exportieren vorhanden.",
            color=COLOR_DOWN
        )
        await interaction.followup.send(embed=embed)
        return
    
    # Generiere Export-Text
    if format.lower() == "detailed":
        export_text = await bot._generate_detailed_export()
    else:
        export_text = await bot._generate_simple_export()
    
    # Erstelle Datei
    try:
        filename = f"futbin_portfolio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        
        embed = discord.Embed(
            title="📤 Portfolio-Export",
            description=f"**Format**: {format.title()}\n**Spieler**: {len(bot.players)}\n**Datei**: `{filename}`",
            color=COLOR_UP,
            timestamp=datetime.now()
        )
        
        # Text als Datei senden
        file = discord.File(
            filename=filename,
            fp=io.StringIO(export_text)
        )
        
        await interaction.followup.send(embed=embed, file=file)
        
    except Exception as e:
        embed = discord.Embed(
            title="❌ Export fehlgeschlagen",
            description=f"Fehler: {str(e)}",
            color=COLOR_DOWN
        )
        await interaction.followup.send(embed=embed)

    async def _generate_simple_export(self):
        """Generiert einfachen Export-Text"""
        lines = []
        lines.append("=== FUTBIN PORTFOLIO EXPORT ===")
        lines.append(f"Datum: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        lines.append(f"Spieler: {len(self.players)}")
        lines.append("")
        
        total_value = 0
        for player in self.players:
            state = self.state.get(player.url, {})
            price = state.get('price', 0)
            total_value += price
            
            line = f"{player.name}: {price:,} Coins"
            if hasattr(player, 'category'):
                line += f" ({player.category})"
            lines.append(line)
        
        lines.append("")
        lines.append(f"Gesamtwert: {total_value:,} Coins")
        return "\n".join(lines)
    
    async def _generate_detailed_export(self):
        """Generiert detaillierten Export-Text"""
        lines = []
        lines.append("=== FUTBIN PORTFOLIO EXPORT (DETAILLIERT) ===")
        lines.append(f"Datum: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        lines.append(f"Spieler: {len(self.players)}")
        lines.append("")
        
        total_value = 0
        categories = {}
        
        for i, player in enumerate(self.players, 1):
            state = self.state.get(player.url, {})
            price = state.get('price', 0)
            min_price = state.get('min_price', price)
            max_price = state.get('max_price', price)
            price_24h = state.get('price_24h_ago', 0)
            
            total_value += price
            category = getattr(player, 'category', 'Allgemein')
            if category not in categories:
                categories[category] = []
            categories[category].append(price)
            
            lines.append(f"{i}. {player.name}")
            lines.append(f"   URL: {player.url}")
            lines.append(f"   Preis: {price:,} Coins")
            lines.append(f"   Min/Max: {min_price:,} - {max_price:,} Coins")
            
            if price_24h > 0:
                change = price - price_24h
                pct = (change / price_24h) * 100
                lines.append(f"   24h: {change:+,} ({pct:+.1f}%)")
            
            lines.append(f"   Kategorie: {category}")
            
            if hasattr(player, 'notes') and player.notes:
                lines.append(f"   Notizen: {player.notes}")
            
            # Alerts
            alerts = []
            if getattr(player, 'alert_above', None):
                alerts.append(f"Über {player.alert_above:,}")
            if getattr(player, 'alert_below', None):
                alerts.append(f"Unter {player.alert_below:,}")
            if alerts:
                lines.append(f"   Alerts: {', '.join(alerts)}")
            
            lines.append("")
        
        # Zusammenfassung
        lines.append("=== ZUSAMMENFASSUNG ===")
        lines.append(f"Gesamtwert: {total_value:,} Coins")
        lines.append(f"Durchschnitt: {total_value//len(self.players):,} Coins")
        lines.append("")
        
        # Kategorien
        lines.append("=== KATEGORIEN ===")
        for cat, prices in sorted(categories.items()):
            cat_value = sum(prices)
            lines.append(f"{cat}: {cat_value:,} Coins ({len(prices)} Spieler)")
        
        return "\n".join(lines)

# Füge auch die fehlende import-Anweisung hinzu
import io

@bot.tree.command(name="debug", description="Zeigt Debug-Informationen an")
async def debug_info(interaction: discord.Interaction):
    """Zeigt Debug-Informationen"""
    await interaction.response.defer(ephemeral=True)
    
    embed = discord.Embed(
        title="🔍 Debug Informationen",
        color=COLOR_NEUTRAL,
        timestamp=datetime.now()
    )
    
    # Dashboard-Status
    dashboard_info = []
    dashboard_info.append(f"**Channel ID**: {DASHBOARD_CHANNEL_ID}")
    dashboard_info.append(f"**Channel Objekt**: {'✅ Geladen' if bot.dashboard_channel else '❌ Nicht gefunden'}")
    dashboard_info.append(f"**Message ID**: {bot.dashboard_message_id or 'Keine'}")
    
    embed.add_field(
        name="📊 Dashboard Status",
        value="\n".join(dashboard_info),
        inline=False
    )
    
    # Spieler-Details
    if bot.players:
        player_details = []
        for i, player in enumerate(bot.players[:5], 1):  # Nur erste 5 zeigen
            state = bot.state.get(player.url, {})
            price = state.get('price', 'Unbekannt')
            last_update = state.get('last_updated', 'Nie')
            player_details.append(f"{i}. **{player.name}**: {price} ({last_update[:10] if last_update != 'Nie' else 'Nie'})")
        
        embed.add_field(
            name="👥 Spieler-Status",
            value="\n".join(player_details),
            inline=False
        )
    
    # System-Info
    system_info = []
    system_info.append(f"**State-Datei**: {'✅ Vorhanden' if bot.state_path.exists() else '❌ Fehlt'}")
    system_info.append(f"**Config-Datei**: {'✅ Vorhanden' if bot.config_path.exists() else '❌ Fehlt'}")
    system_info.append(f"**Geladene States**: {len(bot.state)}")
    
    embed.add_field(
        name="🔧 System",
        value="\n".join(system_info),
        inline=False
    )
    
    # Teste Dashboard-Update
    try:
        if bot.dashboard_channel:
            embed.add_field(
                name="🧪 Test",
                value="Teste Dashboard-Update...",
                inline=False
            )
            await interaction.followup.send(embed=embed)
            
            # Versuche Dashboard zu aktualisieren
            await bot.update_dashboard()
            
            # Follow-up mit Ergebnis
            test_embed = discord.Embed(
                title="✅ Dashboard-Test abgeschlossen",
                description="Dashboard-Update wurde erfolgreich ausgeführt!",
                color=COLOR_UP
            )
            await interaction.followup.send(embed=test_embed)
        else:
            embed.add_field(
                name="❌ Dashboard-Test",
                value="Dashboard Channel nicht verfügbar",
                inline=False
            )
            await interaction.followup.send(embed=embed)
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Dashboard-Test fehlgeschlagen",
            description=f"Fehler: {str(e)}",
            color=COLOR_DOWN
        )
        await interaction.followup.send(embed=error_embed)

if __name__ == "__main__":
    try:
        # Starte Health Check Server in separatem Thread
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        logging.info("Health Check Server Thread gestartet")
        
        # Starte Discord Bot
        bot.run(BOT_TOKEN)
    except Exception as e:
        logging.error(f"Bot konnte nicht gestartet werden: {e}")