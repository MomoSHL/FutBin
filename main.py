"""
FutBin Discord Bot - Refactored Version

A Discord bot for tracking FIFA Ultimate Team player prices from FutBin.
This version uses modular services for better maintainability and performance.
"""

import os
import sys
import time
import asyncio
import logging
import threading
import re
import json
import unicodedata
from pathlib import Path
from typing import List, Optional, Dict, Any
from bs4 import BeautifulSoup

# Windows console UTF-8 setup
if sys.platform == "win32":
    import codecs
    import locale
    
    # Set console to UTF-8 mode
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except:
        # Fallback for older Python versions
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'replace')

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Warning: python-dotenv not installed. Using system environment variables only.")

# Add services to path
sys.path.insert(0, str(Path(__file__).parent))

import discord
from discord.ext import commands, tasks
from datetime import datetime

# Import services
from services import (
    HttpClient, PriceService, StateService, TaskOrchestrator, DashboardService,
    TaskType, PlayerConfig, HAS_PYDANTIC
)
from services.user_dashboard_service import UserDashboardService

# Import config with fallback
if HAS_PYDANTIC:
    from services import load_config, setup_logging
    try:
        CONFIG = load_config()
        setup_logging(CONFIG)
    except Exception as e:
        print(f"Warning: Failed to load pydantic config, using fallback: {e}")
        CONFIG = None
        BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
        if not BOT_TOKEN:
            raise RuntimeError("DISCORD_BOT_TOKEN environment variable is required")
        
        # Setup basic logging with UTF-8 support
        console_handler = logging.StreamHandler()
        if sys.platform == "win32":
            console_handler.stream = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
        
        logging.basicConfig(
            level=logging.INFO,
            format='[%(asctime)s] %(levelname)s [%(name)s]: %(message)s',
            handlers=[
                console_handler,
                logging.FileHandler('logs/bot.log', encoding='utf-8')
            ]
        )
        
        # Configure Discord loggers for UTF-8 support
        for logger_name in ['discord', 'discord.ui', 'discord.ui.view']:
            logger = logging.getLogger(logger_name)
            logger.handlers.clear()  # Remove default handlers
            logger.addHandler(console_handler)
            logger.addHandler(logging.FileHandler('logs/bot.log', encoding='utf-8'))
            logger.propagate = False
else:
    # Fallback configuration without pydantic
    CONFIG = None
    BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is required")
    
    # Setup basic logging with UTF-8 support  
    console_handler = logging.StreamHandler()
    if sys.platform == "win32":
        console_handler.stream = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
    
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s [%(name)s]: %(message)s',
        handlers=[
            console_handler,
            logging.FileHandler('logs/bot.log', encoding='utf-8')
        ]
    )
    
    # Configure Discord loggers for UTF-8 support
    for logger_name in ['discord', 'discord.ui', 'discord.ui.view']:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()  # Remove default handlers
        logger.addHandler(console_handler)
        logger.addHandler(logging.FileHandler('logs/bot.log', encoding='utf-8'))
        logger.propagate = False

from http.server import HTTPServer, BaseHTTPRequestHandler

# Bot reference for health check
bot_instance = None


# Health Check HTTP Server for deployment platforms
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            # Get bot status
            if bot_instance and not bot_instance.is_closed():
                status = {
                    "status": "healthy",
                    "bot_ready": bot_instance.is_ready(),
                    "guilds": len(bot_instance.guilds) if bot_instance.guilds else 0,
                    "players_tracked": len(bot_instance.state_service.get_all_active_players()) if hasattr(bot_instance, 'state_service') else 0
                }
            else:
                status = {"status": "unhealthy", "reason": "bot_not_ready"}
            
            import json
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_error(404)
    
    def log_message(self, format, *args):
        # Suppress default HTTP server logs
        pass


def start_health_server():
    """Start HTTP server for health checks"""
    try:
        port = int(os.environ.get('PORT', 8080))
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logging.info(f"Health Check Server started on port {port}")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Health Check Server could not be started: {e}")


# Dashboard Views and Buttons
class DashboardView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)  # Persistent View
        self.bot = bot_instance

    @discord.ui.button(label="➕ Hinzufügen", style=discord.ButtonStyle.green, custom_id="add_player")
    async def add_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                return
            modal = AddPlayerModal(self.bot)
            await interaction.response.send_modal(modal)
        except discord.errors.NotFound:
            # Interaction expired, ignore
            pass
        except Exception as e:
            logging.error(f"Error in add_player_button: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Fehler beim Öffnen des Spieler-Hinzufügen-Dialogs.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="➖ Entfernen", style=discord.ButtonStyle.red, custom_id="remove_player")
    async def remove_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                return
                
            players = self.bot.state_service.get_all_active_players()
            if not players:
                await interaction.response.send_message("❌ Keine Spieler zum Entfernen vorhanden.", ephemeral=True)
                return
                
            view = RemovePlayerView(self.bot)
            if view.children:  # Check if view has any components
                await interaction.response.send_message("Wähle einen Spieler zum Entfernen:", view=view, ephemeral=True)
            else:
                await interaction.response.send_message("❌ Keine Spieler zum Entfernen vorhanden.", ephemeral=True)
        except discord.errors.NotFound:
            # Interaction expired, ignore
            pass
        except Exception as e:
            logging.error(f"Error in remove_player_button: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Fehler beim Öffnen der Spieler-Entfernung.", ephemeral=True)
            except:
                pass
        
    @discord.ui.button(label="🚨 Alerts", style=discord.ButtonStyle.blurple, custom_id="manage_alerts")
    async def manage_alerts_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                return
            view = AlertManagementView(self.bot, interaction.user.id)
            await interaction.response.send_message("Alert-Verwaltung:", view=view, ephemeral=True)
        except discord.errors.NotFound:
            # Interaction expired, ignore
            pass
        except Exception as e:
            logging.error(f"Error in manage_alerts_button: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Fehler beim Öffnen der Alert-Verwaltung.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="📊 Meine Dashboards", style=discord.ButtonStyle.secondary, custom_id="my_dashboards")
    async def my_dashboards_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show user's personal dashboards in the user dashboard channel"""
        try:
            if interaction.response.is_done():
                return
            
            # Defer interaction
            await interaction.response.defer(ephemeral=True)
            
            user_id = interaction.user.id
            logging.info(f"my_dashboards_button called by user {user_id} ({interaction.user.name})")
            
            # Check if user dashboard channel is configured
            if not self.bot.user_dashboard_channel:
                await interaction.followup.send(
                    "❌ User Dashboard Channel ist nicht konfiguriert.\n"
                    "Ein Admin muss `/set_user_dashboard_channel` verwenden.",
                    ephemeral=True
                )
                return
            
            user_dashboards = self.bot.state_service.get_user_dashboards(user_id)
            logging.info(f"Found {len(user_dashboards)} dashboards for user {user_id}")
            
            if not user_dashboards:
                await interaction.followup.send(
                    "📋 Du hast noch keine Dashboards erstellt.\n"
                    "Verwende `/create_dashboard` um ein neues Dashboard zu erstellen.",
                    ephemeral=True
                )
                return
            
            # Post/update each dashboard in the user dashboard channel
            active_dashboards = [d for d in user_dashboards if d.is_active]
            logging.info(f"Found {len(active_dashboards)} active dashboards for user {user_id}")
            
            if not active_dashboards:
                await interaction.followup.send(
                    "📋 Du hast keine aktiven Dashboards.\n"
                    "Verwende `/my_dashboards` um deine Dashboards zu sehen.",
                    ephemeral=True
                )
                return
            
            posted_count = 0
            
            for dashboard in active_dashboards:
                try:
                    logging.info(f"Posting dashboard '{dashboard.name}' for user {dashboard.owner_id}")
                    # Post or update dashboard message (force_new=True to create if not exists)
                    await self.bot._post_user_dashboard_message(dashboard.name, dashboard.owner_id, force_new=True)
                    posted_count += 1
                    
                except Exception as e:
                    logging.error(f"Error posting dashboard {dashboard.name}: {e}")
                    continue
            
            if posted_count > 0:
                channel_mention = self.bot.user_dashboard_channel.mention
                await interaction.followup.send(
                    f"✅ {posted_count} Dashboard(s) wurden in {channel_mention} erstellt!",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ Keine Dashboards konnten aktualisiert werden.",
                    ephemeral=True
                )
        
        except discord.errors.NotFound:
            # Interaction expired, ignore
            pass
        except Exception as e:
            logging.error(f"Error in my_dashboards_button: {e}", exc_info=True)
            try:
                await interaction.followup.send(
                    "❌ Fehler beim Laden der Dashboards. Bitte versuche es später erneut.",
                    ephemeral=True
                )
            except Exception as followup_error:
                logging.error(f"Error in followup: {followup_error}")
        

class AlertManagementView(discord.ui.View):
    def __init__(self, bot_instance, user_id):
        super().__init__(timeout=300)
        self.bot = bot_instance
        self.user_id = user_id

    @discord.ui.button(label="➕ Alert erstellen", style=discord.ButtonStyle.green)
    async def create_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                return
            modal = CreateAlertModal(self.bot, self.user_id)
            await interaction.response.send_modal(modal)
        except discord.errors.NotFound:
            pass
        except Exception as e:
            logging.error(f"Error in create_alert: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Fehler beim Öffnen des Alert-Dialogs.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="📋 Meine Alerts", style=discord.ButtonStyle.blurple)
    async def list_alerts(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                return
                
            # Find players with actual alerts for this user
            user_alerts = []
            for player in self.bot.state_service.get_all_active_players():
                if player.alert_user_id == self.user_id:
                    alerts = self.bot.state_service.get_player_alerts(player.url)
                    if alerts and ('above' in alerts or 'below' in alerts):
                        user_alerts.append((player, alerts))
            
            if not user_alerts:
                await interaction.response.send_message("Du hast keine aktiven Alerts gesetzt.", ephemeral=True)
                return
            
            # Create alerts embed
            embed = discord.Embed(
                title="📋 Deine aktiven Alerts",
                description=f"Du hast {len(user_alerts)} aktive Alert(s) gesetzt:",
                color=0x3498db
            )
            
            for player, alerts in user_alerts[:10]:  # Limit to 10 alerts
                alert_text = []
                if 'above' in alerts:
                    alert_text.append(f"🔺 Über {alerts['above']:,} Coins")
                if 'below' in alerts:
                    alert_text.append(f"🔻 Unter {alerts['below']:,} Coins")
                
                # Get current price
                state = self.bot.state_service.get_state_by_url(player.url)
                current_price = f" (Aktuell: {state.price:,})" if state and state.price else ""
                
                embed.add_field(
                    name=f"{player.name}{current_price}",
                    value="\n".join(alert_text),
                    inline=True
                )
            
            if len(user_alerts) > 10:
                embed.set_footer(text=f"... und {len(user_alerts) - 10} weitere Alerts")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except discord.errors.NotFound:
            pass
        except Exception as e:
            logging.error(f"Error in list_alerts: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Fehler beim Laden der Alerts.", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="🗑️ Alert löschen", style=discord.ButtonStyle.red)
    async def delete_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.response.is_done():
                return
                
            # Find players with actual alerts for this user
            user_alerts = []
            for player in self.bot.state_service.get_all_active_players():
                if player.alert_user_id == self.user_id:
                    alerts = self.bot.state_service.get_player_alerts(player.url)
                    if alerts and ('above' in alerts or 'below' in alerts):
                        user_alerts.append(player)
            
            if not user_alerts:
                await interaction.response.send_message("Du hast keine Alerts zum Löschen.", ephemeral=True)
                return
            
            view = DeleteAlertView(self.bot, self.user_id, user_alerts)
            if view.children:  # Check if view has any components
                await interaction.response.send_message("Wähle einen Alert zum Löschen:", view=view, ephemeral=True)
            else:
                await interaction.response.send_message("❌ Keine gültigen Alerts zum Löschen gefunden.", ephemeral=True)
                
        except discord.errors.NotFound:
            pass
        except Exception as e:
            logging.error(f"Error in delete_alert: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Fehler beim Laden der Alert-Löschung.", ephemeral=True)
            except:
                pass


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
            # Find player
            player = self.bot.state_service.get_player_by_name(self.player_name.value)
            if not player:
                await interaction.followup.send(
                    f"❌ Spieler '{self.player_name.value}' nicht gefunden.\n"
                    "Verfügbare Spieler: " + ", ".join([p.name for p in self.bot.state_service.get_all_active_players()[:5]]),
                    ephemeral=True
                )
                return
            
            # Parse price
            price_value = self.bot.price_service.parse_alert_price(self.price.value)
            if price_value is None:
                await interaction.followup.send(
                    "❌ Ungültiger Preis. Beispiele:\n• `50k` für 50.000\n• `1.5m` für 1.500.000\n• `32000` für 32.000",
                    ephemeral=True
                )
                return
            
            # Set alert
            alert_type_clean = self.alert_type.value.lower().strip()
            if alert_type_clean in ['über', 'above', 'over']:
                if self.bot.state_service.create_alert(player.url, "above", price_value, self.user_id):
                    alert_desc = f"über {price_value:,} Coins"
                else:
                    await interaction.followup.send("❌ Fehler beim Erstellen des Alerts.", ephemeral=True)
                    return
            elif alert_type_clean in ['unter', 'below', 'under']:
                if self.bot.state_service.create_alert(player.url, "below", price_value, self.user_id):
                    alert_desc = f"unter {price_value:,} Coins"
                else:
                    await interaction.followup.send("❌ Fehler beim Erstellen des Alerts.", ephemeral=True)
                    return
            else:
                await interaction.followup.send(
                    "❌ Ungültiger Alert-Typ. Verwende 'über' oder 'unter'.",
                    ephemeral=True
                )
                return
            
            # Save configuration
            self.bot.state_service.save_dirty_configs()
            
            embed = discord.Embed(
                title="✅ Alert erstellt",
                description=f"**{player.name}**\n\n📊 Alert: {alert_desc}\n💬 Du wirst benachrichtigt wenn der Preis diese Schwelle erreicht.",
                color=0x2ecc71
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error creating alert: {e}")
            await interaction.followup.send(f"❌ Fehler beim Erstellen des Alerts: {e}", ephemeral=True)


class DeleteAlertView(discord.ui.View):
    def __init__(self, bot_instance, user_id, user_alerts):
        super().__init__(timeout=60)
        self.bot = bot_instance
        self.user_id = user_id
        
        # Create dropdown
        options = []
        for i, player in enumerate(user_alerts[:25]):
            alerts = self.bot.state_service.get_player_alerts(player.url)
            alert_info = []
            if 'above' in alerts:
                alert_info.append(f"über {alerts['above']:,}")
            if 'below' in alerts:
                alert_info.append(f"unter {alerts['below']:,}")
            
            # Only add players that actually have alerts
            if alert_info:
                description = "Alerts: " + ", ".join(alert_info)
                options.append(discord.SelectOption(
                    label=player.name[:100],
                    description=description[:100],
                    value=player.url
                ))
        
        if options:
            self.add_item(AlertDeleteSelect(options, self.bot))


class AlertDeleteSelect(discord.ui.Select):
    def __init__(self, options, bot_instance):
        super().__init__(placeholder="Wähle einen Alert zum Löschen...", options=options)
        self.bot = bot_instance

    async def callback(self, interaction: discord.Interaction):
        url = self.values[0]
        player = self.bot.state_service.get_player_by_url(url)
        
        if player:
            # Get current alerts for display
            alerts = self.bot.state_service.get_player_alerts(url)
            old_alerts = []
            if 'above' in alerts:
                old_alerts.append(f"über {alerts['above']:,}")
            if 'below' in alerts:
                old_alerts.append(f"unter {alerts['below']:,}")
            
            # Remove all alerts
            if self.bot.state_service.delete_alert(url, "all"):
                # Save changes
                self.bot.state_service.save_dirty_configs()
                
                embed = discord.Embed(
                    title="✅ Alert gelöscht",
                    description=f"**{player.name}**\n\nEntfernte Alerts:\n" + "\n".join(old_alerts) if old_alerts else "Keine Alerts gefunden",
                    color=0x2ecc71
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message("❌ Fehler beim Löschen der Alerts.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Spieler nicht gefunden.", ephemeral=True)


class AddPlayerModal(discord.ui.Modal):
    def __init__(self, bot_instance):
        super().__init__(title="Spieler hinzufügen")
        self.bot = bot_instance

    url = discord.ui.TextInput(
        label="FutBin URL",
        placeholder="https://www.futbin.com/27/player/234/viktor-gyokeres",
        style=discord.TextStyle.short,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        futbin_url = self.url.value.strip()
        
        # Validate URL
        if not futbin_url.startswith("https://www.futbin.com/"):
            await interaction.followup.send(
                "❌ Ungültige URL. Die URL muss mit `https://www.futbin.com/` beginnen.\n"
                "Beispiel: `https://www.futbin.com/27/player/234/viktor-gyokeres`",
                ephemeral=True
            )
            return
        
        # Check if player already exists
        if self.bot.state_service.get_player_by_url(futbin_url):
            await interaction.followup.send(
                f"❌ Dieser Spieler wird bereits überwacht!\nURL: {futbin_url}",
                ephemeral=True
            )
            return
        
        try:
            # Fetch player data to get name
            price_data = await self.bot.price_service.fetch_player_price(futbin_url)
            
            if not price_data.success or not price_data.name:
                await interaction.followup.send(
                    "❌ Fehler beim Laden der Spielerdaten.\n"
                    f"Fehlermeldung: {price_data.error or 'Unbekannter Fehler'}\n"
                    "Bitte prüfe die URL und versuche es erneut.",
                    ephemeral=True
                )
                return
            
            # Create player config
            new_player = PlayerConfig(
                name=price_data.name,
                url=futbin_url,
                active=True
            )
            
            # Add player
            if self.bot.state_service.add_player(new_player):
                # Initialize state with current data
                if price_data.price:
                    self.bot.state_service.update_player_price(futbin_url, price_data)
                
                # Save changes
                self.bot.state_service.save_dirty_configs()
                self.bot.state_service.save_dirty_states()
                
                embed = discord.Embed(
                    title="✅ Spieler hinzugefügt",
                    description=f"**{price_data.name}** wird jetzt überwacht!\n" +
                                (f"Aktueller Preis: `{price_data.price:,}` Coins" if price_data.price else "Preis wird beim nächsten Update geholt"),
                    color=0x2ecc71
                )
                
                if price_data.image_url:
                    embed.set_thumbnail(url=price_data.image_url)
                
                await interaction.followup.send(embed=embed)
                
                # Trigger dashboard update
                await self.bot.trigger_dashboard_update()
            else:
                await interaction.followup.send("❌ Fehler beim Hinzufügen des Spielers.", ephemeral=True)
        
        except Exception as e:
            logging.error(f"Error adding player: {e}")
            await interaction.followup.send(f"❌ Fehler beim Hinzufügen des Spielers: {e}", ephemeral=True)


class RemovePlayerView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=60)
        self.bot = bot_instance
        
        # Create dropdown with players
        options = []
        players = self.bot.state_service.get_all_active_players()
        
        for i, player in enumerate(players[:25]):  # Discord limit: 25 options
            state = self.bot.state_service.get_state_by_url(player.url)
            price_info = f" ({state.price:,} Coins)" if state and state.price else ""
            
            options.append(discord.SelectOption(
                label=f"{player.name}{price_info}"[:100],
                description=player.category[:100] if hasattr(player, 'category') else "Allgemein"[:100],
                value=player.url
            ))
        
        if options:
            self.add_item(RemovePlayerSelect(options, self.bot))


class RemovePlayerSelect(discord.ui.Select):
    def __init__(self, options, bot_instance):
        super().__init__(placeholder="Wähle einen Spieler zum Entfernen...", options=options)
        self.bot = bot_instance

    async def callback(self, interaction: discord.Interaction):
        url = self.values[0]
        player = self.bot.state_service.get_player_by_url(url)
        
        if player and self.bot.state_service.remove_player(url):
            # Save changes
            self.bot.state_service.save_dirty_configs()
            self.bot.state_service.save_dirty_states()
            
            embed = discord.Embed(
                title="✅ Spieler entfernt",
                description=f"**{player.name}** wird nicht mehr überwacht.",
                color=0x2ecc71
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # Trigger dashboard update
            await self.bot.trigger_dashboard_update()
        else:
            await interaction.response.send_message("❌ Fehler beim Entfernen des Spielers.", ephemeral=True)


# Helper functions for interaction handling
async def safe_interaction_response(interaction: discord.Interaction, content: str = None, embed: discord.Embed = None, 
                                  view: discord.ui.View = None, ephemeral: bool = True):
    """Safely respond to an interaction, handling already acknowledged cases"""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
    except Exception as e:
        logging.error(f"Error in safe_interaction_response: {e}")


def normalize_player_text(text: str) -> str:
    """Normalize text by lowercasing, stripping accents and special characters"""
    if not text:
        return ""
    nfkd = unicodedata.normalize('NFKD', text)
    ascii_text = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_text.lower().strip()


def matches_player_name(query: str, candidate_name: str) -> bool:
    """
    Check if query matches first name, last name, or full name as whole words.
    Example: 'Yamal' matches 'Lamine Yamal', 'Florian' matches 'Florian Wirtz'.
    """
    q_norm = normalize_player_text(query)
    name_norm = normalize_player_text(candidate_name)
    
    if not q_norm or not name_norm:
        return False
        
    if q_norm == name_norm:
        return True
        
    q_words = re.findall(r'[a-z0-9]+', q_norm)
    name_words = re.findall(r'[a-z0-9]+', name_norm)
    
    if not q_words or not name_words:
        return False
        
    # Single word query: matches if it equals one of the words in candidate name
    if len(q_words) == 1:
        return q_words[0] in name_words
        
    # Multi-word query: check if q_words is a sublist of name_words in order
    for i in range(len(name_words) - len(q_words) + 1):
        if name_words[i:i+len(q_words)] == q_words:
            return True
            
    return False


class FutBinBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True
        
        super().__init__(
            command_prefix='!', 
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.watching, name="FutBin Preise 📊")
        )
        
        # Configuration
        self.config = CONFIG
        
        # Initialize services
        self.http_client = HttpClient(
            default_headers=CONFIG.get_headers() if CONFIG else {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
            },
            timeout=CONFIG.network.request_timeout if CONFIG else 12.0,
            max_concurrency=CONFIG.network.max_concurrent_requests if CONFIG else 3
        )
        
        self.price_service = PriceService(
            self.http_client,
            max_concurrent_requests=CONFIG.network.max_concurrent_requests if CONFIG else 3,
            platform=CONFIG.platform if CONFIG else "pc"
        )
        
        self.state_service = StateService(
            state_file_path=CONFIG.state_path if CONFIG else Path("data/player_state.json"),
            config_file_path=CONFIG.config_path if CONFIG else Path("config/bot_config.yaml"),
            global_threshold=CONFIG.global_threshold_percent if CONFIG else 5.0
        )
        
        self.task_orchestrator = TaskOrchestrator(
            max_queue_size=CONFIG.tasks.max_task_queue_size if CONFIG else 100
        )
        
        self.dashboard_service = DashboardService(self.state_service)
        self.user_dashboard_service = UserDashboardService(self.state_service)
        
        # Dashboard tracking
        self.dashboard_channel = None
        self.dashboard_message_id = None
        
        # User Dashboard tracking
        self.user_dashboard_channel = None
        
        # Task health monitoring
        self.last_price_check = None
        self.last_dashboard_update = None
        self.task_errors = {"price_checker": 0, "dashboard_updater": 0}

    async def on_ready(self):
        """Event when bot is ready"""
        global bot_instance
        bot_instance = self
        
        logging.info(f'{self.user} is online and ready!')
        logging.info(f'Bot ID: {self.user.id}')
        logging.info(f'Connected to {len(self.guilds)} server(s)')
        
        # Show server information
        for guild in self.guilds:
            logging.info(f'Server: {guild.name} (ID: {guild.id})')
        
        # Start services
        await self.http_client.start()
        await self.task_orchestrator.start()
        
        # Register task handlers
        self.task_orchestrator.register_handler(TaskType.PRICE_CHECK, self._handle_price_check)
        self.task_orchestrator.register_handler(TaskType.FORCE_PRICE_CHECK, self._handle_price_check)
        self.task_orchestrator.register_handler(TaskType.SINGLE_PLAYER_CHECK, self._handle_single_player_check)
        self.task_orchestrator.register_handler(TaskType.DASHBOARD_UPDATE, self._handle_dashboard_update)
        self.task_orchestrator.register_handler(TaskType.USER_DASHBOARD_UPDATE, self._handle_user_dashboard_update)
        
        # Register persistent views
        self.add_view(DashboardView(self))
        logging.info("Dashboard Views registered")
        
        # Load dashboard channel if set
        dashboard_channel_id = (CONFIG.discord.dashboard_channel_id if CONFIG 
                              else os.getenv('DISCORD_DASHBOARD_CHANNEL_ID'))
        
        if dashboard_channel_id:
            try:
                self.dashboard_channel = self.get_channel(int(dashboard_channel_id))
                if self.dashboard_channel:
                    logging.info(f"Dashboard channel loaded: {self.dashboard_channel.name}")
                    # Try to load existing dashboard message
                    # This would require storing message ID somewhere
                else:
                    logging.warning(f"Dashboard channel {dashboard_channel_id} not found")
            except (ValueError, AttributeError) as e:
                logging.error(f"Invalid dashboard channel ID: {dashboard_channel_id} - {e}")
        
        # Start background tasks
        if not self.price_checker.is_running():
            self.price_checker.start()
            logging.info("Price checker task started")
        if not self.dashboard_updater.is_running():
            self.dashboard_updater.start()
            logging.info("Dashboard updater task started")
        
        # Sync commands
        try:
            synced = await self.tree.sync()
            logging.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logging.error(f"Failed to sync commands: {e}")
    
    async def on_error(self, event_method: str, *args, **kwargs):
        """Global Error Handler for Discord Events"""
        logging.error(f"Discord Event Error in {event_method}: {args}, {kwargs}")
    
    async def on_disconnect(self):
        """Handler for Discord Disconnections"""
        logging.warning("Bot was disconnected from Discord")
        
    async def on_resumed(self):
        """Handler for Discord Reconnections"""
        logging.info("Bot connection to Discord restored")

    async def on_message(self, message: discord.Message):
        """Read messages from Discord chat and check for player sales lookup"""
        # Ignore bot messages
        if message.author.bot:
            return
            
        # Process prefix commands first
        await self.process_commands(message)
        
        content = message.content.strip()
        if not content:
            return
            
        # If it's a command, skip automatic name check
        if content.startswith(('!', '/', '?', '.')):
            return
            
        # Check if message is a player name (1 to 4 words, up to 50 characters)
        words = content.split()
        if 1 <= len(words) <= 4 and len(content) <= 50:
            # Ignore common greetings / conversational chat words
            ignored_words = {
                "hallo", "hi", "hey", "moin", "servus", "guten tag", "bye", "tschüss", 
                "danke", "thx", "thanks", "bitte", "ja", "nein", "yes", "no", 
                "ok", "okay", "gut", "bad", "gg", "lol", "rofl", "lmao", "bruh",
                "test", "ping", "pong", "hilfe", "help"
            }
            if normalize_player_text(content) in ignored_words:
                return
                
            try:
                handled = await self.send_player_sales(message, content)
                if handled:
                    logging.info(f"Sales requested for '{content}' by {message.author} in #{message.channel}")
            except Exception as e:
                logging.error(f"Error handling player sales message for '{content}': {e}")

    async def find_player_for_query(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Find player by first name, last name, or full name.
        1. Check monitored players in config/state
        2. If not found, search Futbin API
        """
        q = query.strip()
        if not q or len(q) < 2:
            return None
            
        # 1. Check local monitored players
        active_players = self.state_service.get_all_active_players()
        for p in active_players:
            if matches_player_name(q, p.name):
                state = self.state_service.get_state_by_url(p.url)
                return {
                    'name': p.name,
                    'url': p.url,
                    'rating': getattr(state, 'card_version', '') or '',
                    'image': state.image if state else None,
                    'is_monitored': True
                }
                
        # Also check all players in state_service.players_by_url
        for url, p in self.state_service.players_by_url.items():
            if matches_player_name(q, p.name):
                state = self.state_service.get_state_by_url(url)
                return {
                    'name': p.name,
                    'url': url,
                    'rating': getattr(state, 'card_version', '') or '',
                    'image': state.image if state else None,
                    'is_monitored': True
                }
                
        # 2. Search Futbin API
        try:
            results = await self.price_service.search_player(q)
            if results:
                # Find the result that matches the query name
                for res in results:
                    if matches_player_name(q, res['name']):
                        return {
                            'name': res['name'],
                            'url': res['url'],
                            'rating': res.get('rating', ''),
                            'version': res.get('version', ''),
                            'image': res.get('image', ''),
                            'is_monitored': False
                        }
                # If no strict word-boundary match, but results were returned and query is an exact substring of first result
                first = results[0]
                q_norm = normalize_player_text(q)
                f_norm = normalize_player_text(first['name'])
                if q_norm in f_norm:
                    return {
                        'name': first['name'],
                        'url': first['url'],
                        'rating': first.get('rating', ''),
                        'version': first.get('version', ''),
                        'image': first.get('image', ''),
                        'is_monitored': False
                    }
        except Exception as e:
            logging.error(f"Error searching player for '{q}': {e}")
            
        return None

    async def send_player_sales(self, target, query: str) -> bool:
        """Fetch and send the last 10 sales of a player on PC platform to message or interaction"""
        player = await self.find_player_for_query(query)
        if not player:
            return False
            
        player_name = player['name']
        player_url = player['url']
        player_image = player.get('image')
        player_rating = player.get('rating', '')
        player_version = player.get('version', '')
        
        # Determine how to send responses
        is_interaction = isinstance(target, discord.Interaction)
        
        if not is_interaction and hasattr(target, 'channel') and hasattr(target.channel, 'typing'):
            try:
                async with target.channel.typing():
                    sales_result = await self.price_service.fetch_player_sales(player_url, platform="pc")
            except Exception:
                sales_result = await self.price_service.fetch_player_sales(player_url, platform="pc")
        else:
            sales_result = await self.price_service.fetch_player_sales(player_url, platform="pc")
            
        if not sales_result.get('success'):
            error_msg = sales_result.get('error', 'Unbekannter Fehler')
            embed = discord.Embed(
                title=f"❌ Keine Verkaufsdaten für {player_name}",
                description=f"Konnte Verkäufe von FutBin (PC) nicht abrufen.\n\n*Fehler:* {error_msg}",
                color=0xe74c3c,
                timestamp=datetime.now()
            )
            embed.set_footer(text="🎮 Plattform: PC • FUTBIN")
            if is_interaction:
                await target.followup.send(embed=embed)
            else:
                await target.reply(embed=embed)
            return True
            
        sales = sales_result.get('sales', [])
        if not sales:
            embed = discord.Embed(
                title=f"🛒 {player_name}",
                description=f"Aktuell sind keine aktuellen Verkaufsdaten auf **PC** für **{player_name}** vorhanden.\n\n[Auf FUTBin ansehen]({player_url})",
                color=0xfa7100,
                timestamp=datetime.now()
            )
            if player_image:
                embed.set_thumbnail(url=player_image)
            embed.set_footer(text="🎮 Plattform: PC • FUTBIN")
            if is_interaction:
                await target.followup.send(embed=embed)
            else:
                await target.reply(embed=embed)
            return True
            
        title_suffix = f" ({player_rating})" if player_rating else ""
        if player_version and player_version.lower() != "normal":
            title_suffix += f" [{player_version}]"
            
        sales_count = len(sales[:10])
        embed = discord.Embed(
            title=f"🛒 Letzte {sales_count} Verkäufe (PC): {player_name}{title_suffix}",
            url=sales_result.get('sales_url', player_url),
            color=0x2ecc71,
            timestamp=datetime.now()
        )
        
        final_image = player_image or sales_result.get('image')
        if final_image:
            embed.set_thumbnail(url=final_image)
            
        # Build table of sales
        has_timestamps = any(s.get('timestamp') for s in sales[:10])
        col2_name = "Datum / Zeit" if has_timestamps else "Eintrag"
        
        table_lines = [
            f"```",
            f"{'Nr.':<4}{col2_name:<18}{'Preis':>11}  {'Trend':<8}",
            f"─" * 43
        ]
        
        for idx, sale in enumerate(sales[:10], 1):
            date_str = sale.get('date') or "Unbekannt"
            price_val = sale.get('price', 0)
            if price_val >= 1_000_000:
                price_str = f"{price_val/1_000_000:.2f}M"
            elif price_val >= 1_000:
                price_str = f"{price_val/1_000:.0f}K"
            else:
                price_str = f"{price_val}"
            price_formatted = f"{price_str} C"
            
            change = sale.get('change')
            if change:
                if change.startswith('+'):
                    trend_str = f"▲ {change}"
                elif change.startswith('-'):
                    trend_str = f"▼ {change}"
                else:
                    trend_str = f"▬ {change}"
            else:
                trend_str = "  -"
                
            table_lines.append(f"{idx:<4}{date_str[:17]:<18}{price_formatted:>11}  {trend_str:<8}")
            
        table_lines.append(f"```")
        
        description_parts = ["\n".join(table_lines)]
        avg_price = sales_result.get('avg_price')
        if avg_price:
            description_parts.append(f"📊 **Durchschnittspreis**: `{avg_price:,} Coins`")
            
        state = self.state_service.get_state_by_url(player_url)
        if state and state.price:
            description_parts.append(f"💵 **Aktueller Tiefstpreis (PC)**: `{state.price:,} Coins`")
            
        embed.description = "\n".join(description_parts)
        embed.set_footer(text="🎮 Plattform: PC • Datenquelle: FUTBIN")
        
        if is_interaction:
            await target.followup.send(embed=embed)
        else:
            await target.reply(embed=embed)
        return True

    # Task handlers
    async def _handle_price_check(self, request):
        """Handle price check tasks"""
        logging.info("Starting price check...")
        start_time = time.time()
        
        players = self.state_service.get_all_active_players()
        if not players:
            logging.info("No players to check")
            return
        
        # Get URLs to check
        urls = [player.url for player in players]
        
        try:
            # Fetch prices concurrently
            price_data = await self.price_service.fetch_multiple_prices(urls)
            
            # Process results
            changes = []
            for url, data in price_data.items():
                player = self.state_service.get_player_by_url(url)
                if player:
                    change_info = self.state_service.update_player_price(url, data)
                    if change_info.get('significant'):
                        changes.append(change_info)
            
            # Save dirty states
            self.state_service.save_dirty_states()
            
            # Send notifications for changes
            if changes:
                await self._send_price_changes(changes)
            
            # Update statistics
            self.last_price_check = time.time()
            execution_time = time.time() - start_time
            
            logging.info(f"Price check completed in {execution_time:.2f}s - {len(changes)} changes detected")
            
            # Trigger updates for all active user dashboards
            await self._trigger_all_user_dashboard_updates()
            
        except Exception as e:
            self.task_errors["price_checker"] += 1
            logging.error(f"Price check failed: {e}")

    async def _handle_single_player_check(self, request):
        """Handle single player check"""
        player_urls = request.data.get('player_urls', [])
        if not player_urls:
            return
        
        logging.info(f"Checking {len(player_urls)} specific players")
        
        try:
            price_data = await self.price_service.fetch_multiple_prices(player_urls)
            
            changes = []
            for url, data in price_data.items():
                player = self.state_service.get_player_by_url(url)
                if player:
                    change_info = self.state_service.update_player_price(url, data)
                    if change_info:
                        changes.append(change_info)
            
            self.state_service.save_dirty_states()
            
            if changes:
                await self._send_price_changes(changes)
            
            # Trigger updates for affected user dashboards
            await self._trigger_affected_user_dashboard_updates(player_urls)
                
        except Exception as e:
            logging.error(f"Single player check failed: {e}")

    async def _handle_dashboard_update(self, request):
        """Handle dashboard update tasks"""
        if not self.dashboard_channel:
            return
        
        logging.info("Updating dashboard...")
        
        try:
            # Create new embed with ORIGINAL dashboard format
            embed_data = await self.dashboard_service.create_dashboard_embed()
            
            # Convert dict to discord.Embed object
            embed = discord.Embed(
                title=embed_data.get('title', '📊 Dashboard'),
                description=embed_data.get('description', 'Keine Daten verfügbar'),
                color=embed_data.get('color', 0xfa7100),
                timestamp=datetime.fromisoformat(embed_data.get('timestamp', datetime.now().isoformat()))
            )
            
            # Add fields if present
            for field in embed_data.get('fields', []):
                embed.add_field(
                    name=field['name'],
                    value=field['value'], 
                    inline=field.get('inline', True)
                )
            
            # Add footer if present
            if embed_data.get('footer'):
                embed.set_footer(text=embed_data['footer']['text'])
            
            view = DashboardView(self)
            
            # Try to edit existing message (only the latest one)
            if self.dashboard_message_id:
                try:
                    message = await self.dashboard_channel.fetch_message(self.dashboard_message_id)
                    await message.edit(embed=embed, view=view)
                    logging.info("Dashboard message updated")
                except discord.NotFound:
                    # Message was deleted, create new one
                    message = await self.dashboard_channel.send(embed=embed, view=view)
                    self.dashboard_message_id = message.id
                    logging.info("Created new dashboard message (old message not found)")
            else:
                # Create new message
                message = await self.dashboard_channel.send(embed=embed, view=view)
                self.dashboard_message_id = message.id
                logging.info("Created new dashboard message")
            
            self.last_dashboard_update = time.time()
            
        except Exception as e:
            self.task_errors["dashboard_updater"] += 1
            logging.error(f"Dashboard update failed: {e}")

    async def _send_price_changes(self, changes):
        """Send price change notifications"""
        if not self.dashboard_channel or not changes:
            return
        
        try:
            if len(changes) > 5:
                # Send bulk notification
                await self._send_bulk_price_changes(changes)
            else:
                # Send individual notifications
                await self._send_individual_price_changes(changes)
        except Exception as e:
            logging.error(f"Failed to send price changes: {e}")

    async def _send_bulk_price_changes(self, changes):
        """Send many changes in one embed"""
        embed = discord.Embed(
            title=f"🔔 Preisänderungen ({len(changes)})",
            color=0xfa7100,
            timestamp=datetime.now()
        )
        
        description_lines = []
        for change in changes[:20]:  # Limit to prevent too long embeds
            player = change['player']
            old_price = change['old_price']
            new_price = change['new_price']
            change_pct = change['change_percent']
            
            emoji = "📈" if change_pct > 0 else "📉"
            description_lines.append(
                f"{emoji} **{player.name}**: `{old_price:,}` → `{new_price:,}` ({change_pct:+.1f}%)"
            )
        
        if len(changes) > 20:
            description_lines.append(f"*...und {len(changes) - 20} weitere Änderungen*")
        
        embed.description = "\n".join(description_lines)
        await self.dashboard_channel.send(embed=embed)

    async def _send_individual_price_changes(self, changes):
        """Send few changes as individual messages"""
        for change in changes:
            player = change['player']
            old_price = change['old_price']
            new_price = change['new_price']
            change_pct = change['change_percent']
            
            color = 0x2ecc71 if change_pct > 0 else 0xe74c3c
            emoji = "📈" if change_pct > 0 else "📉"
            
            embed = discord.Embed(
                title=f"{emoji} {player.name}",
                color=color,
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="Preisänderung",
                value=f"`{old_price:,}` → `{new_price:,}` Coins\n**{change_pct:+.1f}%**",
                inline=True
            )
            
            # Add image if available
            state = self.state_service.get_state_by_url(player.url)
            if state and state.image:
                embed.set_thumbnail(url=state.image)
            
            await self.dashboard_channel.send(embed=embed)

    # Helper methods for external triggering
    async def trigger_price_check(self, force: bool = False, player_urls: list = None):
        """Trigger price check via orchestrator"""
        return await self.task_orchestrator.trigger_price_check(force, player_urls)

    async def trigger_dashboard_update(self, force: bool = False):
        """Trigger dashboard update via orchestrator"""
        return await self.task_orchestrator.trigger_dashboard_update(force)

    async def _trigger_all_user_dashboard_updates(self):
        """Trigger updates for all active user dashboards after price check - only for already posted dashboards"""
        try:
            updated_count = 0
            for dashboard_key, dashboard in self.state_service.user_dashboards.items():
                # Only update dashboards that have already been posted (have a message ID)
                if (dashboard.is_active and
                    len(dashboard.player_urls) > 0 and
                    dashboard.dashboard_message_id is not None):
                    await self.task_orchestrator.trigger_user_dashboard_update(
                        dashboard.name,
                        dashboard.owner_id
                    )
                    updated_count += 1
            
            if updated_count > 0:
                logging.info(f"Triggered updates for {updated_count} user dashboards")
        except Exception as e:
            logging.error(f"Error triggering user dashboard updates: {e}")

    async def _trigger_affected_user_dashboard_updates(self, player_urls: list):
        """Trigger updates only for user dashboards that contain the updated players - only for already posted dashboards"""
        try:
            affected_dashboards = set()
            
            # Find which dashboards contain these players
            for dashboard_key, dashboard in self.state_service.user_dashboards.items():
                if not dashboard.is_active:
                    continue
                
                # Only update dashboards that have already been posted (have a message ID)
                if dashboard.dashboard_message_id is None:
                    continue
                
                # Check if any of the updated players are in this dashboard
                for player_url in player_urls:
                    if player_url in dashboard.player_urls:
                        affected_dashboards.add((dashboard.name, dashboard.owner_id))
                        break
            
            # Trigger updates for affected dashboards
            for dashboard_name, owner_id in affected_dashboards:
                await self.task_orchestrator.trigger_user_dashboard_update(
                    dashboard_name, 
                    owner_id
                )
            
            if affected_dashboards:
                logging.info(f"Triggered updates for {len(affected_dashboards)} affected user dashboards")
        except Exception as e:
            logging.error(f"Error triggering affected dashboard updates: {e}")

    # Synchronous versions for direct execution (e.g., refresh button)
    async def _handle_price_check_sync(self):
        """Synchronous price check for immediate execution"""
        logging.info("Starting synchronous price check...")
        start_time = time.time()
        
        players = self.state_service.get_all_active_players()
        if not players:
            logging.info("No players to check")
            return
        
        # Get URLs to check
        urls = [player.url for player in players]
        
        try:
            # Fetch prices concurrently
            price_data = await self.price_service.fetch_multiple_prices(urls)
            
            # Process results
            changes = []
            for url, data in price_data.items():
                player = self.state_service.get_player_by_url(url)
                if player:
                    change_info = self.state_service.update_player_price(url, data)
                    if change_info.get('significant'):
                        changes.append(change_info)
            
            # Save dirty states
            self.state_service.save_dirty_states()
            
            execution_time = time.time() - start_time
            logging.info(f"Sync price check completed in {execution_time:.2f}s - {len(changes)} changes detected")
            
            # Trigger updates for all active user dashboards
            await self._trigger_all_user_dashboard_updates()
            
            return len(changes)
            
        except Exception as e:
            logging.error(f"Sync price check failed: {e}")
            raise

    async def _handle_dashboard_update_sync(self):
        """Synchronous dashboard update for immediate execution"""
        if not self.dashboard_channel:
            logging.warning("No dashboard channel configured")
            return
        
        logging.info("Starting synchronous dashboard update...")
        
        try:
            # Create new embed with ORIGINAL dashboard format
            embed_data = await self.dashboard_service.create_dashboard_embed()
            
            # Convert dict to discord.Embed object
            embed = discord.Embed(
                title=embed_data.get('title', '📊 Dashboard'),
                description=embed_data.get('description', 'Keine Daten verfügbar'),
                color=embed_data.get('color', 0xfa7100),
                timestamp=datetime.fromisoformat(embed_data.get('timestamp', datetime.now().isoformat()))
            )
            
            # Add fields if present
            for field in embed_data.get('fields', []):
                embed.add_field(
                    name=field['name'],
                    value=field['value'], 
                    inline=field.get('inline', True)
                )
            
            # Add footer if present
            if embed_data.get('footer'):
                embed.set_footer(text=embed_data['footer']['text'])
            
            view = DashboardView(self)
            
            # Delete old message first if exists
            if self.dashboard_message_id:
                try:
                    old_message = await self.dashboard_channel.fetch_message(self.dashboard_message_id)
                    await old_message.delete()
                    logging.info("Deleted old dashboard message")
                except discord.NotFound:
                    # Message already deleted
                    pass
                except Exception as e:
                    logging.warning(f"Could not delete old dashboard message: {e}")
            
            # Create completely new dashboard message
            message = await self.dashboard_channel.send(embed=embed, view=view)
            self.dashboard_message_id = message.id
            logging.info("Created brand new dashboard message synchronously")
            
        except Exception as e:
            logging.error(f"Sync dashboard update failed: {e}")
            raise

    async def _handle_user_dashboard_update_sync(self, dashboard_name: str, owner_id: int):
        """Synchronous user dashboard update for immediate execution - SAME as main dashboard sync"""
        if not self.user_dashboard_channel:
            logging.debug("User dashboard channel not set, skipping sync update")
            return
        
        logging.info(f"Starting synchronous user dashboard update: {dashboard_name}")
        
        try:
            # Get dashboard
            dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
            if not dashboard:
                logging.warning(f"Dashboard not found: {dashboard_name}")
                return
            
            # Create dashboard embed (SAME as regular update)
            embed = self.user_dashboard_service.format_dashboard_embed(dashboard_name, owner_id)
            if not embed:
                logging.warning(f"Failed to create embed for dashboard {dashboard_name}")
                return
            
            # Create view with buttons (SAME as regular update)
            view = UserDashboardView(self, dashboard_name, owner_id)
            
            # Edit existing message if it exists
            if dashboard.dashboard_message_id:
                try:
                    message = await self.user_dashboard_channel.fetch_message(dashboard.dashboard_message_id)
                    await message.edit(embed=embed, view=view)
                    logging.info(f"User dashboard message updated synchronously: {dashboard_name}")
                    
                    # Update timestamp AFTER successful edit
                    dashboard.last_updated = datetime.now().isoformat()
                    self.state_service._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
                    
                except discord.NotFound:
                    # Message was deleted, clear the message ID
                    logging.info(f"User dashboard message not found for {dashboard_name}, clearing message_id")
                    dashboard.dashboard_message_id = None
                    dashboard.dashboard_channel_id = None
                    self.state_service._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
            else:
                logging.debug(f"Dashboard {dashboard_name} has no message_id, skipping sync update")
            
        except Exception as e:
            logging.error(f"Sync user dashboard update failed for {dashboard_name}: {e}")
            raise

    async def _handle_user_dashboard_update(self, request):
        """Handle user dashboard update tasks - SAME structure as main dashboard update"""
        try:
            data = request.data or {}
            dashboard_name = data.get('dashboard_name')
            owner_id = data.get('owner_id')
            
            if not dashboard_name or not owner_id:
                logging.warning("User dashboard update request missing required data")
                return
            
            # Check if user dashboard channel is set
            if not self.user_dashboard_channel:
                logging.debug("User dashboard channel not set, skipping update")
                return
            
            logging.info(f"Updating user dashboard: {dashboard_name} for user {owner_id}")
            
            # Get dashboard
            dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
            if not dashboard:
                logging.warning(f"Dashboard not found: {dashboard_name}")
                return
            
            # Skip if dashboard has no message ID (hasn't been posted yet)
            if not dashboard.dashboard_message_id:
                logging.debug(f"Dashboard {dashboard_name} has no message_id, skipping update")
                return
            
            # Create dashboard embed (SAME as main dashboard)
            embed = self.user_dashboard_service.format_dashboard_embed(dashboard_name, owner_id)
            if not embed:
                logging.warning(f"Failed to create embed for dashboard {dashboard_name}")
                return
            
            # Create view with buttons (SAME as main dashboard)
            view = UserDashboardView(self, dashboard_name, owner_id)
            
            # Try to edit existing message (SAME as main dashboard)
            if dashboard.dashboard_message_id:
                try:
                    message = await self.user_dashboard_channel.fetch_message(dashboard.dashboard_message_id)
                    await message.edit(embed=embed, view=view)
                    logging.info(f"User dashboard message updated: {dashboard_name}")
                    
                    # Update timestamp AFTER successful edit
                    dashboard.last_updated = datetime.now().isoformat()
                    self.state_service._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
                    self.state_service.save_all()
                    
                except discord.NotFound:
                    # Message was deleted, clear the message ID so it can be recreated later
                    logging.info(f"User dashboard message not found for {dashboard_name}, clearing message_id")
                    dashboard.dashboard_message_id = None
                    dashboard.dashboard_channel_id = None
                    self.state_service._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
                    self.state_service.save_all()
            
        except Exception as e:
            logging.error(f"User dashboard update failed: {e}")
            self.task_errors['user_dashboard_update'] = self.task_errors.get('user_dashboard_update', 0) + 1
    
    async def _post_user_dashboard_message(self, dashboard_name: str, owner_id: int, force_new: bool = False):
        """Post or update user dashboard message in the user dashboard channel
        
        Args:
            dashboard_name: Name of the dashboard
            owner_id: Owner's user ID
            force_new: If True, always create a NEW message (deletes old one first). 
            If False, only update existing messages (don't create new ones).
        """
        try:
            if not self.user_dashboard_channel:
                return
            
            # Get dashboard
            dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
            if not dashboard:
                return
            
            # Create dashboard embed
            embed = self.user_dashboard_service.format_dashboard_embed(dashboard_name, owner_id)
            if not embed:
                logging.warning(f"Failed to create embed for dashboard {dashboard_name}")
                return
            
            # Create view with buttons
            view = UserDashboardView(self, dashboard_name, owner_id)
            
            old_message_id = getattr(dashboard, 'dashboard_message_id', None)
            old_channel_id = getattr(dashboard, 'dashboard_channel_id', None)
            
            if force_new:
                # Force new message: Delete old message if exists, then create new one
                if old_message_id and old_channel_id:
                    try:
                        # Try to delete old message
                        old_channel = self.get_channel(old_channel_id) or self.user_dashboard_channel
                        old_message = await old_channel.fetch_message(old_message_id)
                        await old_message.delete()
                        logging.info(f"Deleted old user dashboard message for {dashboard_name}")
                    except discord.NotFound:
                        # Message already deleted
                        pass
                    except Exception as e:
                        logging.warning(f"Could not delete old user dashboard message: {e}")
                
                # Create new message
                message = await self.user_dashboard_channel.send(
                    embed=embed, 
                    view=view
                )
                
                # Store message ID in dashboard
                dashboard.dashboard_message_id = message.id
                dashboard.dashboard_channel_id = self.user_dashboard_channel.id
                self.state_service._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
                self.state_service.save_all()
                
                logging.info(f"Posted NEW user dashboard message for {dashboard_name} (message ID: {message.id})")
                
            else:
                # Update only: Only edit existing messages, don't create new ones
                if not old_message_id or not old_channel_id:
                    logging.debug(f"No message ID for {dashboard_name} and force_new=False, skipping")
                    return
                
                try:
                    # Check if channel changed
                    if old_channel_id != self.user_dashboard_channel.id:
                        logging.info(f"Channel changed for {dashboard_name} but force_new=False, skipping")
                        return
                    
                    # Try to edit existing message
                    message = await self.user_dashboard_channel.fetch_message(old_message_id)
                    await message.edit(embed=embed, view=view)
                    logging.info(f"Updated existing user dashboard message for {dashboard_name}")
                    
                except discord.NotFound:
                    # Message was deleted, but we're not allowed to create new one
                    logging.info(f"Old message not found for {dashboard_name} but force_new=False, skipping")
                    return
                except Exception as e:
                    logging.warning(f"Could not edit user dashboard message: {e}")
                    return
            
        except Exception as e:
            logging.error(f"Failed to post user dashboard message: {e}")

    async def _delete_user_dashboard_message(self, dashboard_name: str, owner_id: int):
        """Delete user dashboard message from the user dashboard channel
        
        Args:
            dashboard_name: Name of the dashboard
            owner_id: Owner's user ID
        """
        try:
            # Get dashboard
            dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
            if not dashboard:
                return
            
            message_id = getattr(dashboard, 'dashboard_message_id', None)
            channel_id = getattr(dashboard, 'dashboard_channel_id', None)
            
            if message_id and channel_id:
                try:
                    # Try to find the channel
                    channel = self.get_channel(channel_id)
                    if not channel:
                        logging.warning(f"Channel {channel_id} not found for dashboard {dashboard_name}")
                        return
                    
                    # Try to fetch and delete the message
                    message = await channel.fetch_message(message_id)
                    await message.delete()
                    logging.info(f"Deleted user dashboard message for {dashboard_name} (message ID: {message_id})")
                    
                except discord.NotFound:
                    # Message already deleted
                    logging.debug(f"Message {message_id} not found (already deleted)")
                except Exception as e:
                    logging.warning(f"Could not delete user dashboard message: {e}")
            else:
                logging.debug(f"No message ID stored for dashboard {dashboard_name}")
                
        except Exception as e:
            logging.error(f"Failed to delete user dashboard message: {e}")

    @tasks.loop(seconds=280)  # Default interval, will be overridden by config
    async def price_checker(self):
        """Background task that triggers price checks"""
        await self.trigger_price_check()
    
    @tasks.loop(seconds=300)  # Default interval, will be overridden by config
    async def dashboard_updater(self):
        """Background task that triggers dashboard updates"""
        await self.trigger_dashboard_update()
    
    async def close(self):
        """Clean shutdown"""
        logging.info("Shutting down bot...")
        
        # Stop services
        await self.task_orchestrator.stop()
        await self.http_client.close()
        
        # Save any pending data
        self.state_service.save_dirty_configs()
        self.state_service.save_dirty_states()
        
        await super().close()


# Bot instance
bot = FutBinBot()


# Slash Commands
@bot.tree.command(name="setup", description="Complete FutBin Bot setup")
async def setup_bot(interaction: discord.Interaction):
    """Perform complete bot setup"""
    await interaction.response.defer()
    
    embed = discord.Embed(
        title="🔧 FutBin Bot Setup",
        color=0xfa7100,
        timestamp=datetime.now()
    )
    
    setup_steps = []
    
    # 1. Configure dashboard channel
    bot.dashboard_channel = interaction.channel
    setup_steps.append("✅ Dashboard channel configured")
    
    # 2. Check existing players
    player_count = len(bot.state_service.get_all_active_players())
    if player_count > 0:
        setup_steps.append(f"✅ {player_count} players already configured")
    else:
        setup_steps.append("ℹ️ No players configured yet - use buttons below to add some")
    
    # 3. Create dashboard
    try:
        await bot.trigger_dashboard_update(force=True)
        setup_steps.append("✅ Dashboard created")
    except Exception as e:
        setup_steps.append(f"⚠️ Dashboard creation failed: {e}")
    
    # 4. Check background tasks
    if bot.price_checker.is_running():
        setup_steps.append("✅ Price checker running")
    else:
        setup_steps.append("⚠️ Price checker not running")
    
    if bot.dashboard_updater.is_running():
        setup_steps.append("✅ Dashboard updater running")
    else:
        setup_steps.append("⚠️ Dashboard updater not running")
    
    embed.description = "\n".join(setup_steps)
    embed.add_field(
        name="📚 Next Steps",
        value=(
            "• Use the **buttons** in the dashboard to manage players\n"
            "• Or use `/add_player <url>` for command-based management\n"
            "• The dashboard updates automatically every 5 minutes\n"
            "• Prices are checked every ~4.5 minutes\n"
            "• Example URL: `https://www.futbin.com/27/player/234/viktor-gyokeres`"
        ),
        inline=False
    )
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="set_dashboard", description="Set current channel as dashboard channel")
async def set_dashboard(interaction: discord.Interaction):
    """Set dashboard channel"""
    bot.dashboard_channel = interaction.channel
    
    embed = discord.Embed(
        title="✅ Dashboard configured",
        description=f"This channel has been set as the dashboard channel.\nThe live dashboard will be updated here every {CONFIG.tasks.dashboard_update_interval//60 if CONFIG else 5} minutes.",
        color=0xfa7100
    )
    
    await interaction.response.send_message(embed=embed)
    
    # Create dashboard immediately
    await bot.trigger_dashboard_update(force=True)


@bot.tree.command(name="dashboard", description="Create new dashboard (deletes old one)")
async def refresh_dashboard(interaction: discord.Interaction):
    """Create new dashboard and delete old one"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Delete old message if exists
        if bot.dashboard_message_id and bot.dashboard_channel:
            try:
                old_message = await bot.dashboard_channel.fetch_message(bot.dashboard_message_id)
                await old_message.delete()
                logging.info("Deleted old dashboard message")
            except discord.NotFound:
                pass  # Message already deleted
        
        # Create new dashboard
        await bot.trigger_dashboard_update(force=True)
        
        embed = discord.Embed(
            title="✅ Dashboard aktualisiert",
            description="Ein neues Dashboard wurde erstellt.",
            color=0x2ecc71
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        logging.error(f"Error refreshing dashboard: {e}")
        await interaction.followup.send(f"❌ Fehler beim Aktualisieren des Dashboards: {e}", ephemeral=True)


@bot.tree.command(name="set_user_dashboard_channel", description="Setze Channel für User Dashboard Updates")
async def set_user_dashboard_channel(interaction: discord.Interaction):
    """Set the channel for user dashboard updates"""
    bot.user_dashboard_channel = interaction.channel
    
    embed = discord.Embed(
        title="✅ User Dashboard Channel konfiguriert",
        description=f"Dieser Channel wurde als User Dashboard Channel gesetzt.\nAlle User Dashboard Updates werden hier gepostet.",
        color=0x2ecc71
    )
    
    await interaction.response.send_message(embed=embed)
    
    logging.info(f"User dashboard channel set to: {interaction.channel.name} (ID: {interaction.channel.id})")


@bot.tree.command(name="add_player", description="Add player for monitoring")
async def add_player(interaction: discord.Interaction, futbin_url: str):
    """Add new player (automatic name recognition)"""
    
    # Validate URL
    if not futbin_url.startswith("https://www.futbin.com/"):
        await interaction.response.send_message(
            "❌ Invalid URL. URL must start with `https://www.futbin.com/`\n"
            "Example: `https://www.futbin.com/27/player/234/viktor-gyokeres`",
            ephemeral=True
        )
        return
    
    # Check if player already exists
    if bot.state_service.get_player_by_url(futbin_url):
        await interaction.response.send_message(
            f"❌ This player is already being monitored!\nURL: {futbin_url}",
            ephemeral=True
        )
        return
    
    # Test URL
    await interaction.response.defer()
    
    try:
        price_data = await bot.price_service.fetch_player_price(futbin_url)
        
        if not price_data.success or not price_data.name:
            await interaction.followup.send(
                "❌ Error loading player data.\n"
                f"Error: {price_data.error or 'Unknown error'}\n"
                "Please check the URL and try again.",
                ephemeral=True
            )
            return
        
        # Add player
        new_player = PlayerConfig(name=price_data.name, url=futbin_url, active=True)
        
        if bot.state_service.add_player(new_player):
            # Initialize state
            if price_data.price:
                bot.state_service.update_player_price(futbin_url, price_data)
            
            # Save changes
            bot.state_service.save_dirty_configs()
            bot.state_service.save_dirty_states()
            
            embed = discord.Embed(
                title="✅ Player added",
                description=f"**{price_data.name}** is now being monitored!\n" + 
                           (f"Current price: `{price_data.price:,}` Coins" if price_data.price else "Price will be fetched in next update"),
                color=0x2ecc71
            )
            
            if price_data.image_url:
                embed.set_thumbnail(url=price_data.image_url)
            
            await interaction.followup.send(embed=embed)
            
            # Update dashboard
            if bot.dashboard_channel:
                await bot.trigger_dashboard_update()
        else:
            await interaction.followup.send("❌ Error adding player.", ephemeral=True)
    
    except Exception as e:
        logging.error(f"Error adding player: {e}")
        await interaction.followup.send(f"❌ Error adding player: {e}", ephemeral=True)


@bot.tree.command(name="remove_player", description="Remove player from monitoring")
async def remove_player(interaction: discord.Interaction, name: str):
    """Remove a player"""
    
    # Find player
    player_to_remove = bot.state_service.get_player_by_name(name)
    
    if not player_to_remove:
        available_players = [p.name for p in bot.state_service.get_all_active_players()]
        await interaction.response.send_message(
            f"❌ Player '{name}' not found.\n" +
            (f"Available players: {', '.join(available_players[:5])}" if available_players else "No players configured."),
            ephemeral=True
        )
        return
    
    # Remove player
    if bot.state_service.remove_player(player_to_remove.url):
        bot.state_service.save_dirty_configs()
        bot.state_service.save_dirty_states()
        
        embed = discord.Embed(
            title="✅ Player removed",
            description=f"**{player_to_remove.name}** is no longer monitored.",
            color=0x2ecc71
        )
        
        await interaction.response.send_message(embed=embed)
        
        # Update dashboard
        if bot.dashboard_channel:
            await bot.trigger_dashboard_update()
    else:
        await interaction.response.send_message("❌ Error removing player.", ephemeral=True)


@bot.tree.command(name="list_players", description="Show all monitored players")
async def list_players(interaction: discord.Interaction):
    """List all players"""
    
    players = bot.state_service.get_all_active_players()
    
    if not players:
        await interaction.response.send_message(
            "📭 No players being monitored.\n"
            "Use `/add_player <url>` or the dashboard buttons to add players.",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title=f"📋 Monitored Players ({len(players)})",
        color=0xfa7100
    )
    
    description_lines = []
    for i, player in enumerate(players, 1):
        state = bot.state_service.get_state_by_url(player.url)
        price_info = f" - `{state.price:,}` Coins" if state and state.price else " - Preis unbekannt"
        description_lines.append(f"{i}. **{player.name}**{price_info}")
    
    embed.description = "\n".join(description_lines)
    embed.set_footer(text="Use /remove_player <name> to remove a player")
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="force_update", description="Force immediate dashboard update")
async def force_update(interaction: discord.Interaction):
    """Force dashboard update"""
    await interaction.response.defer()
    
    if not bot.dashboard_channel:
        await interaction.followup.send(
            "❌ No dashboard channel configured.\n"
            "Use `/set_dashboard` in the channel where you want the dashboard.",
            ephemeral=True
        )
        return
    
    # Trigger price update
    await bot.trigger_price_check(force=True)
    
    # Update dashboard
    await bot.trigger_dashboard_update(force=True)
    
    embed = discord.Embed(
        title="✅ Update completed",
        description="Dashboard has been updated!",
        color=0x2ecc71
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="forceupdate", description="Aktualisiere alle aktuell angezeigten Dashboards sofort")
async def forceupdate_all_dashboards(interaction: discord.Interaction):
    """Force update all currently posted dashboards (main + all user dashboards)"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        updated_dashboards = 0
        errors = []
        
        # 1. Update prices first (for all dashboards)
        try:
            await bot._handle_price_check_sync()
            logging.info("Prices updated for forceupdate command")
        except Exception as e:
            logging.error(f"Error updating prices: {e}")
            errors.append(f"Price Update: {str(e)[:50]}")
        
        # 2. Update main dashboard if configured
        if bot.dashboard_channel and bot.dashboard_message_id:
            try:
                # Update main dashboard (synchronously)
                await bot._handle_dashboard_update_sync()
                updated_dashboards += 1
                logging.info("Main dashboard force updated")
            except Exception as e:
                logging.error(f"Error force updating main dashboard: {e}")
                errors.append(f"Main Dashboard: {str(e)[:50]}")
        
        # 3. Update all user dashboards that have been posted (have message IDs)
        user_dashboard_count = 0
        for dashboard_key, dashboard in bot.state_service.user_dashboards.items():
            if (dashboard.is_active and
                len(dashboard.player_urls) > 0 and
                dashboard.dashboard_message_id is not None):
                try:
                    # Use the synchronous update function (SAME as main dashboard)
                    await bot._handle_user_dashboard_update_sync(dashboard.name, dashboard.owner_id)
                    user_dashboard_count += 1
                    updated_dashboards += 1
                    logging.info(f"User dashboard force updated: {dashboard.name} (owner: {dashboard.owner_id})")
                except Exception as e:
                    logging.error(f"Error force updating user dashboard {dashboard.name}: {e}")
                    errors.append(f"{dashboard.name}: {str(e)[:50]}")
        
        # Save all changes
        bot.state_service.save_all()
        
        # Create response embed
        embed = discord.Embed(
            title="✅ Force Update abgeschlossen",
            description=f"**{updated_dashboards} Dashboard(s) wurden aktualisiert!**",
            color=0x2ecc71,
            timestamp=datetime.now()
        )
        
        # Add details
        details = []
        if bot.dashboard_message_id:
            details.append("📊 Main Dashboard aktualisiert")
        if user_dashboard_count > 0:
            details.append(f"📋 {user_dashboard_count} User Dashboard(s) aktualisiert")
        
        if details:
            embed.add_field(
                name="🔄 Aktualisiert",
                value="\n".join(details),
                inline=False
            )
        
        # Show errors if any
        if errors:
            error_text = "\n".join([f"• {err}" for err in errors[:5]])
            if len(errors) > 5:
                error_text += f"\n...und {len(errors) - 5} weitere Fehler"
            embed.add_field(
                name="⚠️ Fehler",
                value=error_text,
                inline=False
            )
            embed.color = 0xff9900  # Orange if there were errors
        
        if updated_dashboards == 0:
            embed.title = "⚠️ Keine Dashboards aktualisiert"
            embed.description = (
                "Es wurden keine Dashboards aktualisiert.\n\n"
                "**Mögliche Gründe:**\n"
                "• Keine Dashboards wurden bisher gepostet\n"
                "• Dashboard-Channel nicht konfiguriert\n"
                "• Alle Dashboards wurden gelöscht"
            )
            embed.color = 0xff9900  # Orange for warning
        
        embed.set_footer(text=f"Angefordert von {interaction.user.name}")
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logging.error(f"Error in forceupdate command: {e}", exc_info=True)
        await interaction.followup.send(
            f"❌ Fehler beim Force Update: {str(e)[:200]}",
            ephemeral=True
        )


@bot.tree.command(name="info", description="Show bot information and status")
async def bot_info(interaction: discord.Interaction):
    """Show bot information"""
    embed = discord.Embed(
        title="ℹ️ FutBin Bot Information",
        color=0xfa7100,
        timestamp=datetime.now()
    )
    
    # Status information
    status_lines = []
    status_lines.append(f"🤖 **Bot Status**: Online")
    status_lines.append(f"📊 **Dashboard Channel**: {f'<#{bot.dashboard_channel.id}>' if bot.dashboard_channel else 'Nicht konfiguriert'}")
    status_lines.append(f"👥 **Überwachte Spieler**: {len(bot.state_service.get_all_active_players())}")
    status_lines.append(f"🔄 **Price Checker**: {'🟢 Aktiv' if bot.price_checker.is_running() else '🔴 Inaktiv'}")
    status_lines.append(f"📈 **Dashboard Updater**: {'🟢 Aktiv' if bot.dashboard_updater.is_running() else '🔴 Inaktiv'}")
    
    # User Dashboard info
    user_dashboard_count = len([d for d in bot.state_service.user_dashboards.values() if d.is_active])
    status_lines.append(f"📋 **User Dashboards**: {user_dashboard_count}")
    status_lines.append(f"🎯 **User Dashboard Channel**: {f'<#{bot.user_dashboard_channel.id}>' if bot.user_dashboard_channel else 'Nicht konfiguriert'}")
    
    embed.add_field(
        name="📊 Status",
        value="\n".join(status_lines),
        inline=False
    )
    
    # Configuration
    config_lines = []
    if CONFIG:
        config_lines.append(f"🎮 **Platform**: {CONFIG.platform.upper()}")
        config_lines.append(f"📊 **Price Threshold**: {CONFIG.global_threshold_percent}%")
        config_lines.append(f"⏱️ **Update Interval**: {CONFIG.tasks.dashboard_update_interval//60} minutes")
        config_lines.append(f"🔍 **Price Check**: {CONFIG.tasks.price_check_interval} seconds")
    else:
        config_lines.append("⚠️ **Configuration**: Using fallback settings")
    
    embed.add_field(
        name="⚙️ Configuration",
        value="\n".join(config_lines),
        inline=False
    )
    
    # Commands - Split into multiple fields to avoid 1024 char limit
    
    # Field 1: Haupt-Dashboard & User Dashboards
    commands_field_1 = (
        "**🏠 Haupt-Dashboard:**\n"
        "`/setup` - Bot-Setup\n"
        "`/set_dashboard` - Channel setzen\n"
        "`/dashboard` - Neues Dashboard\n"
        "`/add_player <url>` - Spieler hinzufügen\n"
        "`/remove_player <name>` - Spieler entfernen\n"
        "`/list_players` - Alle Spieler\n"
        "`/force_update` - Sofort-Update\n\n"
        "**📋 User Dashboards:**\n"
        "`/create_dashboard <name>` - Dashboard erstellen\n"
        "`/my_dashboards` - Deine Dashboards\n"
        "`/show_dashboard <name>` - Dashboard anzeigen\n"
        "`/delete_dashboard <name>` - Dashboard löschen"
    )
    
    # Field 2: User Dashboard Management & Preise
    commands_field_2 = (
        "**📋 Dashboard Management:**\n"
        "`/add_to_dashboard` - Spieler hinzufügen\n"
        "`/remove_from_dashboard` - Spieler entfernen\n"
        "`/list_dashboards` - Alle Dashboards\n"
        "`/set_user_dashboard_channel` - Channel setzen\n\n"
        "**💰 Preise & Info:**\n"
        "`/price <spieler>` - Preis abfragen (Autocomplete)\n"
        "`/sales <spieler>` - Letzte 10 Verkäufe (PC)\n"
        "💬 Chat: Schreib einfach einen Spielernamen in den Chat!\n"
        "`/info` - Bot-Informationen\n"
        "`/stats` - Statistiken\n\n"
        "**🎮 Interaktiv:**\n"
        "Dashboard Buttons für Spieler & Alerts\n"
        "💰 Kaufpreise verwalten"
    )
    
    embed.add_field(
        name="🔧 Commands (1/2)",
        value=commands_field_1,
        inline=False
    )
    
    embed.add_field(
        name="🔧 Commands (2/2)",
        value=commands_field_2,
        inline=False
    )
    
    # Add admin section if user is admin
    if is_admin(interaction.user.id):
        admin_info = (
            "**⚙️ Admin (Nur für dich):**\n"
            "`/admin` - Admin Control Panel\n"
            "• Bot Neustarten/Ausschalten\n"
            "• Backups, Logs, Stats\n"
            "• Force Updates & Cache"
        )
        embed.add_field(
            name="� Admin",
            value=admin_info,
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Show orchestrator and performance statistics")
async def show_stats(interaction: discord.Interaction):
    """Show bot statistics"""
    stats = bot.task_orchestrator.get_stats()
    
    embed = discord.Embed(
        title="📊 Bot Statistics",
        color=0xfa7100,
        timestamp=datetime.now()
    )
    
    # Task statistics
    task_lines = []
    task_lines.append(f"**Tasks Executed**: {stats['tasks_executed']}")
    task_lines.append(f"**Tasks Failed**: {stats['tasks_failed']}")
    task_lines.append(f"**Queue Size**: {stats['queue_size']}")
    task_lines.append(f"**Queue Overflows**: {stats['queue_overflows']}")
    task_lines.append(f"**Running Tasks**: {len(stats['running_tasks'])}")
    
    embed.add_field(
        name="🎯 Task Orchestrator",
        value="\n".join(task_lines),
        inline=True
    )
    
    # Performance statistics
    perf_lines = []
    if bot.last_price_check:
        import time
        last_check_ago = int(time.time() - bot.last_price_check)
        perf_lines.append(f"**Last Price Check**: {last_check_ago}s ago")
    
    if bot.last_dashboard_update:
        import time
        last_update_ago = int(time.time() - bot.last_dashboard_update)
        perf_lines.append(f"**Last Dashboard Update**: {last_update_ago}s ago")
    
    perf_lines.append(f"**Price Check Errors**: {bot.task_errors.get('price_checker', 0)}")
    perf_lines.append(f"**Dashboard Errors**: {bot.task_errors.get('dashboard_updater', 0)}")
    
    embed.add_field(
        name="⚡ Performance",
        value="\n".join(perf_lines) if perf_lines else "No data available",
        inline=True
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="check_market", description="Überprüfe den Status der Datenquellen und Cloudflare-Bypass")
async def check_market_cmd(interaction: discord.Interaction):
    """Diagnose data provider connectivity and Cloudflare status"""
    await interaction.response.defer(ephemeral=True)

    t0 = time.time()
    search_test = await bot.price_service.search_player("Musiala")
    t_search = int((time.time() - t0) * 1000)

    t1 = time.time()
    sales_test = await bot.price_service.fetch_player_sales("https://www.futbin.com/27/player/257/florian-wirtz", platform="pc")
    t_sales = int((time.time() - t1) * 1000)

    flaresolverr_url = os.getenv('FLARESOLVERR_URL')
    proxy = os.getenv('FUTBIN_PROXY') or os.getenv('MARKET_PROXY') or os.getenv('HTTP_PROXY')

    embed = discord.Embed(
        title="🌐 EA FC / FutBin Verbindungsstatus",
        color=0x2ecc71 if (search_test and sales_test.get('success')) else 0xe74c3c,
        timestamp=datetime.now()
    )

    embed.add_field(
        name="🔍 Such-API",
        value=f"✅ OK ({len(search_test)} Treffer, {t_search}ms)" if search_test else f"❌ 0 Treffer / Blockiert ({t_search}ms)",
        inline=True
    )

    embed.add_field(
        name="📈 Live Verkäufe",
        value=f"✅ OK ({len(sales_test.get('sales', []))} Sales, {t_sales}ms)" if sales_test.get('success') else f"❌ Fehler: {str(sales_test.get('error', 'Unbekannt'))[:40]}",
        inline=True
    )

    embed.add_field(
        name="🛡️ Cloudflare Bypass",
        value=f"✅ FlareSolverr aktiv (`{flaresolverr_url}`)" if flaresolverr_url else "⚡ Direkt / curl_cffi Profile",
        inline=False
    )

    if proxy:
        embed.add_field(
            name="🔒 Proxy",
            value=f"`{proxy.split('@')[-1]}`",
            inline=True
        )

    embed.add_field(
        name="🎮 Plattform",
        value=bot.price_service.platform.upper(),
        inline=True
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


# ==============================================
# PRICE COMMAND
# ==============================================


async def tracked_player_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[discord.app_commands.Choice[str]]:
    """
    Autocomplete für Spielernamen aus getrackte Spielern
    """
    # Get all tracked players (from main dashboard + all user dashboards)
    all_players = bot.state_service.get_all_active_players()
    
    if not current:
        # Return top 25 tracked players if no input
        return [
            discord.app_commands.Choice(
                name=f"{player.name}"[:100],
                value=player.url[:100]
            )
            for player in all_players[:25]
        ]
    
    # Filter players by name (case-insensitive)
    current_lower = current.lower()
    filtered = [
        p for p in all_players 
        if current_lower in p.name.lower()
    ]
    
    # Return up to 25 matches
    return [
        discord.app_commands.Choice(
            name=f"{player.name}"[:100],
            value=player.url[:100]
        )
        for player in filtered[:25]
    ]


@bot.tree.command(name="price", description="Zeige aktuellen Preis eines Spielers")
@discord.app_commands.autocomplete(spieler=tracked_player_autocomplete)
@discord.app_commands.describe(
    spieler="Spielername (Autocomplete) oder FUTBin URL"
)
async def price_command(interaction: discord.Interaction, spieler: str):
    """
    Zeige aktuellen FUTBin-Preis eines Spielers
    
    Unterstützt:
    1. Autocomplete-Auswahl aus getrackten Spielern
    2. Direkte FUTBin URL-Eingabe
    """
    await interaction.response.defer()
    
    try:
        # Check if input is a FUTBin URL or player selection
        if spieler.startswith("https://www.futbin.com/"):
            futbin_url = spieler
            
            # Try to find player in tracked list
            player_config = bot.state_service.get_player_by_url(futbin_url)
            player_name = player_config.name if player_config else "Unbekannter Spieler"
        else:
            # It's a player name from autocomplete, need to find URL
            player_config = bot.state_service.get_player_by_name(spieler)
            
            if not player_config:
                # Try to find by URL (autocomplete might return URL)
                player_config = bot.state_service.get_player_by_url(spieler)
            
            if not player_config:
                await interaction.followup.send(
                    f"❌ Spieler **{spieler}** nicht gefunden.\n"
                    "Verwende die Autocomplete-Funktion oder gib eine vollständige FUTBin URL ein:\n"
                    "`https://www.futbin.com/27/player/234/viktor-gyokeres`",
                    ephemeral=True
                )
                return
            
            futbin_url = player_config.url
            player_name = player_config.name
        
        # Fetch current price from FUTBin
        price_data = await bot.price_service.fetch_player_price(futbin_url)
        
        if not price_data.success:
            await interaction.followup.send(
                f"❌ Fehler beim Abrufen des Preises für **{player_name}**\n"
                f"URL: {futbin_url}\n"
                f"Fehler: {price_data.error or 'Unbekannter Fehler'}",
                ephemeral=True
            )
            return
        
        # Create price embed
        embed = discord.Embed(
            title=f"💰 {player_name}",
            color=0xfa7100,
            timestamp=datetime.now()
        )
        
        # Price information
        if price_data.price:
            embed.add_field(
                name="💵 Aktueller Preis",
                value=f"**{price_data.price:,}** Coins",
                inline=True
            )
        else:
            embed.add_field(
                name="💵 Preis",
                value="Nicht verfügbar",
                inline=True
            )
        
        # Additional info if player is tracked
        if player_config:
            state = bot.state_service.get_state_by_url(futbin_url)
            if state:
                # Show price change if available
                if state.previous_price and state.price:
                    change = state.price - state.previous_price
                    change_pct = (change / state.previous_price * 100) if state.previous_price else 0
                    
                    change_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"
                    embed.add_field(
                        name=f"{change_emoji} Preis-Änderung",
                        value=f"{change:+,} Coins ({change_pct:+.1f}%)",
                        inline=True
                    )
                
                # Show if tracked in dashboards
                tracking_info = []
                
                # Check main dashboard
                if player_config in bot.state_service.get_all_active_players():
                    tracking_info.append("📊 Haupt-Dashboard")
                
                # Check user dashboards
                for dashboard_key, dashboard in bot.state_service.user_dashboards.items():
                    if futbin_url in dashboard.player_urls:
                        tracking_info.append(f"📋 {dashboard.name}")
                
                if tracking_info:
                    embed.add_field(
                        name="📍 Getrackt in",
                        value="\n".join(tracking_info[:5]),  # Max 5
                        inline=False
                    )
        
        # Add player image if available
        if price_data.image_url:
            embed.set_thumbnail(url=price_data.image_url)
        
        # Add FUTBin link
        embed.add_field(
            name="🔗 FUTBin",
            value=f"[Zur Spieler-Seite]({futbin_url})",
            inline=False
        )
        
        embed.set_footer(text="Daten von FUTBin • PC-Preise")
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logging.error(f"Error in price command: {e}")
        try:
            await interaction.followup.send(
                f"❌ Unerwarteter Fehler beim Abrufen des Preises: {e}",
                ephemeral=True
            )
        except:
            pass


@bot.tree.command(name="sales", description="Zeige die letzten 10 Verkäufe eines Spielers auf dem PC-Markt")
@discord.app_commands.autocomplete(spieler=tracked_player_autocomplete)
@discord.app_commands.describe(
    spieler="Spielername (Vor- oder Nachname) oder FUTBin URL"
)
async def sales_command(interaction: discord.Interaction, spieler: str):
    """
    Zeige die letzten 10 Verkäufe eines Spielers auf FUTBin (PC-Plattform).
    """
    await interaction.response.defer()
    try:
        handled = await bot.send_player_sales(interaction, spieler)
        if not handled:
            await interaction.followup.send(
                f"❌ Spieler **{spieler}** nicht gefunden.\n"
                "Gib den Vor- oder Nachnamen ein (z. B. `Yamal`, `Konate`, `Musiala`) oder eine FUTBin URL.",
                ephemeral=True
            )
    except Exception as e:
        logging.error(f"Error in sales command: {e}")
        await interaction.followup.send(f"❌ Fehler beim Abrufen der Verkäufe: {e}", ephemeral=True)


@bot.command(name="sales", aliases=["verkäufe", "verkaeufe"])
async def prefix_sales_command(ctx: commands.Context, *, spieler: str):
    """Text-Command: !sales <Spielername>"""
    try:
        handled = await bot.send_player_sales(ctx.message, spieler)
        if not handled:
            await ctx.reply(
                f"❌ Spieler **{spieler}** nicht gefunden.\n"
                "Gib den Vor- oder Nachnamen ein (z. B. `Yamal`, `Konate`, `Musiala`) oder eine FUTBin URL."
            )
    except Exception as e:
        logging.error(f"Error in prefix sales command: {e}")
        await ctx.reply(f"❌ Fehler: {e}")


# ==============================================
# ADMIN DASHBOARD
# ==============================================

# Admin User IDs - Only these users can access admin functions
ADMIN_USER_IDS = [870045115169771531, 416353640073134082]

def is_admin(user_id: int) -> bool:
    """Check if user is an admin"""
    return user_id in ADMIN_USER_IDS


class AdminDashboardView(discord.ui.View):
    """Admin control panel with all administrative functions"""
    
    def __init__(self, bot_instance):
        super().__init__(timeout=300)
        self.bot = bot_instance
    
    @discord.ui.button(label="🔄 Bot Neustarten", style=discord.ButtonStyle.danger, row=0)
    async def restart_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Restart the bot"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        # Confirm restart
        embed = discord.Embed(
            title="⚠️ Bot Neustart",
            description="Möchtest du den Bot wirklich neu starten?\n\n"
                    "Der Bot wird heruntergefahren und muss manuell neu gestartet werden.",
            color=0xff9900
        )
        
        view = ConfirmRestartView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label="🛑 Bot Ausschalten", style=discord.ButtonStyle.danger, row=0)
    async def shutdown_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Shutdown the bot"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        # Confirm shutdown
        embed = discord.Embed(
            title="⚠️ Bot Ausschalten",
            description="Möchtest du den Bot wirklich ausschalten?\n\n"
                        "Der Bot wird beendet und muss manuell neu gestartet werden.",
            color=0xff0000
        )
        
        view = ConfirmShutdownView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    
    
    @discord.ui.button(label="🔄 Daten neu laden", style=discord.ButtonStyle.secondary, row=1)
    async def reload_data(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Reload all data from files"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Reload state and config
            self.bot.state_service.load_all()
            
            embed = discord.Embed(
                title="✅ Daten neu geladen",
                description="Alle Konfigurationen und States wurden von den Dateien neu geladen.",
                color=0x00ff00
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error reloading data: {e}")
            await interaction.followup.send(f"❌ Fehler beim Neuladen der Daten: {e}", ephemeral=True)
    
    @discord.ui.button(label="💾 Daten speichern", style=discord.ButtonStyle.secondary, row=1)
    async def save_data(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Force save all data"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Force save all
            self.bot.state_service.save_all()
            
            embed = discord.Embed(
                title="✅ Daten gespeichert",
                description="Alle Daten wurden erfolgreich gespeichert.",
                color=0x00ff00
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error saving data: {e}")
            await interaction.followup.send(f"❌ Fehler beim Speichern: {e}", ephemeral=True)
    
    @discord.ui.button(label="🔄 Force Update", style=discord.ButtonStyle.secondary, row=1)
    async def force_update_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Force update all dashboards and prices"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            updated_dashboards = 0
            errors = []
            
            # Trigger price check first
            try:
                await self.bot._handle_price_check_sync()
                logging.info("Prices updated via admin force update")
            except Exception as e:
                logging.error(f"Error updating prices: {e}")
                errors.append(f"Price Update: {str(e)[:50]}")
            
            # Update main dashboard
            if self.bot.dashboard_channel and self.bot.dashboard_message_id:
                try:
                    await self.bot._handle_dashboard_update_sync()
                    updated_dashboards += 1
                except Exception as e:
                    logging.error(f"Error updating main dashboard: {e}")
                    errors.append(f"Main Dashboard: {str(e)[:50]}")
            
            # Update all active user dashboards that have been posted (SAME as main dashboard)
            user_dashboard_count = 0
            for dashboard_key, dashboard in self.bot.state_service.user_dashboards.items():
                if (dashboard.is_active and
                    len(dashboard.player_urls) > 0 and
                    dashboard.dashboard_message_id is not None):
                    try:
                        # Use synchronous update function (SAME structure as main dashboard)
                        await self.bot._handle_user_dashboard_update_sync(dashboard.name, dashboard.owner_id)
                        user_dashboard_count += 1
                        updated_dashboards += 1
                    except Exception as e:
                        logging.error(f"Error updating user dashboard {dashboard.name}: {e}")
                        errors.append(f"{dashboard.name}: {str(e)[:50]}")
            
            # Save changes
            self.bot.state_service.save_all()
            
            # Create response
            embed = discord.Embed(
                title="✅ Force Update abgeschlossen",
                description=f"**{updated_dashboards} Dashboard(s) aktualisiert**",
                color=0x00ff00
            )
            
            details = []
            if self.bot.dashboard_message_id:
                details.append("📊 Main Dashboard")
            if user_dashboard_count > 0:
                details.append(f"📋 {user_dashboard_count} User Dashboard(s)")
            
            if details:
                embed.add_field(
                    name="🔄 Aktualisiert",
                    value="\n".join(details),
                    inline=False
                )
            
            if errors:
                error_text = "\n".join([f"• {err}" for err in errors[:5]])
                if len(errors) > 5:
                    error_text += f"\n...und {len(errors) - 5} weitere"
                embed.add_field(
                    name="⚠️ Fehler",
                    value=error_text,
                    inline=False
                )
                embed.color = 0xff9900
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error in force update: {e}")
            await interaction.followup.send(f"❌ Fehler beim Update: {e}", ephemeral=True)



    @discord.ui.button(label="🗑️ Cache leeren", style=discord.ButtonStyle.secondary, row=1)
    async def clear_cache(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Clear various caches"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Clear HTTP client cache if exists
            if hasattr(self.bot.http_client, 'clear_cache'):
                self.bot.http_client.clear_cache()
            
            # Reset task errors
            self.bot.task_errors = {"price_checker": 0, "dashboard_updater": 0}
            
            embed = discord.Embed(
                title="✅ Cache geleert",
                description="Cache und Error-Counter wurden zurückgesetzt.",
                color=0x00ff00
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error clearing cache: {e}")
            await interaction.followup.send(f"❌ Fehler beim Leeren des Cache: {e}", ephemeral=True)
            
            
    @discord.ui.button(label="💾 Backup erstellen", style=discord.ButtonStyle.primary, row=2)
    async def create_backup(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Create backup of all data"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            import shutil
            from datetime import datetime
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = Path(f"backups/backup_{timestamp}")
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Backup data files
            data_files = [
                "data/player_state.json",
                "data/user_dashboards.json",
                "config/bot_config.yaml"
            ]
            
            backed_up = []
            for file_path in data_files:
                src = Path(file_path)
                if src.exists():
                    dst = backup_dir / src.name
                    shutil.copy2(src, dst)
                    backed_up.append(src.name)
            
            embed = discord.Embed(
                title="✅ Backup erstellt",
                description=f"**Backup-Ordner:** `{backup_dir}`\n\n"
                            f"**Gesicherte Dateien:**\n" + "\n".join([f"• {f}" for f in backed_up]),
                color=0x00ff00,
                timestamp=datetime.now()
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error creating backup: {e}")
            await interaction.followup.send(f"❌ Fehler beim Erstellen des Backups: {e}", ephemeral=True)
            
            
    @discord.ui.button(label="📊 System Stats", style=discord.ButtonStyle.primary, row=2)
    async def system_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show detailed system statistics"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            import psutil
            import platform
            
            # System info
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Bot stats
            uptime = time.time() - bot_start_time if 'bot_start_time' in globals() else 0
            uptime_hours = uptime / 3600
            
            embed = discord.Embed(
                title="📊 System Statistiken",
                color=0x0099ff,
                timestamp=datetime.now()
            )
            
            # System info
            embed.add_field(
                name="💻 System",
                value=(
                    f"**OS:** {platform.system()} {platform.release()}\n"
                    f"**Python:** {platform.python_version()}\n"
                    f"**CPU:** {cpu_percent}%\n"
                    f"**RAM:** {memory.percent}% ({memory.used / (1024**3):.1f} GB / {memory.total / (1024**3):.1f} GB)\n"
                    f"**Disk:** {disk.percent}% ({disk.used / (1024**3):.1f} GB / {disk.total / (1024**3):.1f} GB)"
                ),
                inline=False
            )
            
            # Bot stats
            embed.add_field(
                name="🤖 Bot",
                value=(
                    f"**Uptime:** {uptime_hours:.1f}h\n"
                    f"**Servers:** {len(self.bot.guilds)}\n"
                    f"**Spieler getrackt:** {len(self.bot.state_service.get_all_active_players())}\n"
                    f"**User Dashboards:** {len([d for d in self.bot.state_service.user_dashboards.values() if d.is_active])}\n"
                    f"**Latency:** {self.bot.latency * 1000:.0f}ms"
                ),
                inline=False
            )
            
            # Task stats
            task_stats = self.bot.task_orchestrator.get_stats()
            embed.add_field(
                name="⚙️ Tasks",
                value=(
                    f"**Executed:** {task_stats['tasks_executed']}\n"
                    f"**Failed:** {task_stats['tasks_failed']}\n"
                    f"**Queue:** {task_stats['queue_size']}\n"
                    f"**Running:** {len(task_stats['running_tasks'])}"
                ),
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error getting system stats: {e}")
            await interaction.followup.send(f"❌ Fehler beim Abrufen der Statistiken: {e}", ephemeral=True)
    
    @discord.ui.button(label="📝 Logs anzeigen", style=discord.ButtonStyle.primary, row=2)
    async def show_logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show recent log entries"""
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            log_file = Path("logs/bot.log")
            
            if not log_file.exists():
                await interaction.followup.send("❌ Log-Datei nicht gefunden.", ephemeral=True)
                return
            
            # Read last 50 lines (with error handling for encoding issues)
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                last_lines = lines[-50:] if len(lines) > 50 else lines
            
            log_content = "".join(last_lines)
            
            # Truncate if too long
            if len(log_content) > 1900:
                log_content = "..." + log_content[-1900:]
            
            embed = discord.Embed(
                title="📝 Letzte Log-Einträge",
                description=f"```\n{log_content}\n```",
                color=0x0099ff,
                timestamp=datetime.now()
            )
            
            embed.set_footer(text=f"Zeige letzte {len(last_lines)} Zeilen")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error reading logs: {e}")
            await interaction.followup.send(f"❌ Fehler beim Lesen der Logs: {e}", ephemeral=True)
    
    


class ConfirmRestartView(discord.ui.View):
    """Confirmation view for bot restart"""
    
    def __init__(self, bot_instance):
        super().__init__(timeout=60)
        self.bot = bot_instance
    
    @discord.ui.button(label="✅ Ja, neu starten", style=discord.ButtonStyle.danger)
    async def confirm_restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🔄 Bot wird neu gestartet...",
            description="Der Bot fährt jetzt herunter. Bitte starte ihn manuell neu.",
            color=0xff9900
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Save all data before restart
        self.bot.state_service.save_all()
        
        logging.info(f"Bot restart initiated by {interaction.user.name} ({interaction.user.id})")
        
        # Restart bot
        await self.bot.close()
        os.execv(sys.executable, ['python'] + sys.argv)
    
    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel_restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Neustart abgebrochen.", ephemeral=True)


class ConfirmShutdownView(discord.ui.View):
    """Confirmation view for bot shutdown"""
    
    def __init__(self, bot_instance):
        super().__init__(timeout=60)
        self.bot = bot_instance
    
    @discord.ui.button(label="✅ Ja, ausschalten", style=discord.ButtonStyle.danger)
    async def confirm_shutdown(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🛑 Bot wird ausgeschaltet...",
            description="Der Bot fährt jetzt herunter. Auf Wiedersehen! 👋",
            color=0xff0000
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Save all data before shutdown
        self.bot.state_service.save_all()
        
        logging.info(f"Bot shutdown initiated by {interaction.user.name} ({interaction.user.id})")
        
        # Shutdown bot
        await self.bot.close()
        sys.exit(0)
    
    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel_shutdown(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ausschalten abgebrochen.", ephemeral=True)


@bot.tree.command(name="admin", description="Admin Control Panel (Nur für Administratoren)")
async def admin_command(interaction: discord.Interaction):
    """Open admin dashboard - Admins only"""
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ Du hast keine Berechtigung für diesen Command.\n"
            "Dieser Command ist nur für Bot-Administratoren verfügbar.",
            ephemeral=True
        )
        logging.warning(f"Unauthorized admin access attempt by {interaction.user.name} ({interaction.user.id})")
        return
    
    embed = discord.Embed(
        title="⚙️ Admin Control Panel",
        color=0xff0000,
        timestamp=datetime.now()
    )
    
    embed.set_footer(text=f"Admin: {interaction.user.name}")
    
    view = AdminDashboardView(bot)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    logging.info(f"Admin dashboard opened by {interaction.user.name} ({interaction.user.id})")


@bot.tree.command(name="cleanup_duplicates", description="Entferne doppelte Spieler aus dem Main Dashboard (Nur Admins)")
async def cleanup_duplicates_command(interaction: discord.Interaction):
    """Remove duplicate players from Main Dashboard - Admins only"""
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ Du hast keine Berechtigung für diesen Command.\n"
            "Dieser Command ist nur für Bot-Administratoren verfügbar.",
            ephemeral=True
        )
        logging.warning(f"Unauthorized cleanup_duplicates access attempt by {interaction.user.name} ({interaction.user.id})")
        return
    
    # Defer response as this might take a while
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Find duplicates
        duplicates = bot.state_service.find_duplicate_players()
        
        if not duplicates:
            embed = discord.Embed(
                title="✅ Keine Duplikate gefunden",
                description="Es wurden keine doppelten Spieler im Main Dashboard gefunden.",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Show what was found
        embed_preview = discord.Embed(
            title="🔍 Duplikate gefunden",
            description=f"Es wurden **{len(duplicates)}** Gruppen mit doppelten Spielern gefunden:",
            color=0xffaa00,
            timestamp=datetime.now()
        )
        
        for norm_url, urls in list(duplicates.items())[:10]:  # Show first 10
            players = [bot.state_service.players_by_url[url].name for url in urls]
            embed_preview.add_field(
                name=f"🔸 {players[0]}",
                value=f"Gefunden: {len(urls)}x\n└ URLs: {', '.join([url.split('/')[-1] for url in urls])}",
                inline=False
            )
        
        if len(duplicates) > 10:
            embed_preview.add_field(
                name="...",
                value=f"Und {len(duplicates) - 10} weitere Gruppen",
                inline=False
            )
        
        await interaction.followup.send(embed=embed_preview, ephemeral=True)
        
        # Merge duplicates
        merge_stats = bot.state_service.merge_duplicate_players(duplicates)
        
        # Save changes
        bot.state_service.save_all()
        
        # Show results
        embed_result = discord.Embed(
            title="✅ Duplikate bereinigt",
            description="Die doppelten Spieler wurden erfolgreich zusammengeführt.",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        
        embed_result.add_field(
            name="📊 Statistik",
            value=(
                f"**Gruppen verarbeitet:** {merge_stats['groups_processed']}\n"
                f"**Spieler behalten:** {merge_stats['players_kept']}\n"
                f"**Duplikate entfernt:** {merge_stats['players_removed']}"
            ),
            inline=False
        )
        
        # Show details of merged players
        if merge_stats['details']:
            detail_text = ""
            for detail in merge_stats['details'][:5]:  # Show first 5
                kept = detail['kept']
                removed = detail['removed']
                detail_text += f"**{kept['name']}**\n"
                detail_text += f"└ Behalten: {kept['url'].split('/')[-1]}\n"
                detail_text += f"└ Entfernt: {len(removed)}x ({', '.join([r['url'].split('/')[-1] for r in removed])})\n\n"
            
            if len(merge_stats['details']) > 5:
                detail_text += f"_...und {len(merge_stats['details']) - 5} weitere_"
            
            embed_result.add_field(
                name="🔸 Details",
                value=detail_text or "Keine Details verfügbar",
                inline=False
            )
        
        embed_result.set_footer(text=f"Admin: {interaction.user.name}")
        
        await interaction.followup.send(embed=embed_result, ephemeral=True)
        
        logging.info(
            f"Duplicates cleaned up by {interaction.user.name} ({interaction.user.id}): "
            f"{merge_stats['players_removed']} duplicates removed, {merge_stats['players_kept']} players kept"
        )
        
    except Exception as e:
        logging.error(f"Error in cleanup_duplicates: {e}", exc_info=True)
        
        embed_error = discord.Embed(
            title="❌ Fehler beim Bereinigen",
            description=f"Es ist ein Fehler aufgetreten:\n```{str(e)}```",
            color=0xff0000,
            timestamp=datetime.now()
        )
        
        await interaction.followup.send(embed=embed_error, ephemeral=True)


# Track bot start time for uptime calculation
bot_start_time = time.time()


# ==============================================
# USER DASHBOARD VIEWS
# ==============================================

class UserDashboardView(discord.ui.View):
    """Interactive view for user dashboard - public viewing with owner-only editing"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
    
    @discord.ui.button(label="➕ Hinzufügen", style=discord.ButtonStyle.success, row=0)
    async def add_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add player to dashboard - owner only"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"❌ Nur <@{self.owner_id}> kann Spieler zu diesem Dashboard hinzufügen.", 
                ephemeral=True
            )
            return
        
        # Show add player modal
        modal = AddPlayerToDashboardModal(self.bot, self.dashboard_name, self.owner_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🗑️ Entfernen", style=discord.ButtonStyle.danger, row=0)
    async def remove_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Remove player from dashboard - owner only"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"❌ Nur <@{self.owner_id}> kann Spieler von diesem Dashboard entfernen.", 
                ephemeral=True
            )
            return
        
        # Show remove player view
        view = RemovePlayerFromDashboardView(self.bot, self.dashboard_name, self.owner_id)
        embed = discord.Embed(
            title="➖ Entfernen",
            description=f"Wähle einen Spieler aus, den du vom Dashboard **{self.dashboard_name}** entfernen möchtest:",
            color=0xff9900
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        
    
    '''
    @discord.ui.button(label="🔔 Benachrichtigungen", style=discord.ButtonStyle.secondary, row=1)
    async def manage_alerts(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Manage alerts for dashboard players - owner only"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"❌ Nur <@{self.owner_id}> kann Benachrichtigungen für dieses Dashboard verwalten.", 
                ephemeral=True
            )
            return
        
        # Show alert management for dashboard players
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard or not dashboard.player_urls:
            await interaction.response.send_message("❌ Keine Spieler in diesem Dashboard.", ephemeral=True)
            return
        
        view = DashboardAlertManagementView(self.bot, self.dashboard_name, self.owner_id)
        embed = discord.Embed(
            title="🔔 Benachrichtigungen verwalten",
            description=f"Verwalte Benachrichtigungen für Spieler in **{self.dashboard_name}**:",
            color=0x0099ff
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    '''
    
    @discord.ui.button(label="💰 Gekauft für", style=discord.ButtonStyle.secondary, row=0)
    async def manage_purchase_prices(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Manage purchase prices for dashboard players - owner only"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"❌ Nur <@{self.owner_id}> kann Kaufpreise für dieses Dashboard verwalten.", 
                ephemeral=True
            )
            return
        
        # Show purchase price management for dashboard players
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard or not dashboard.player_urls:
            await interaction.response.send_message("❌ Keine Spieler in diesem Dashboard.", ephemeral=True)
            return
        
        view = PurchasePriceManagementView(self.bot, self.dashboard_name, self.owner_id)
        embed = discord.Embed(
            title="💰 Kaufpreise verwalten",
            description=f"Verwalte Kaufpreise für Spieler in **{self.dashboard_name}**:\n\n"
                        f"➕ Kaufpreis setzen\n"
                        f"✏️ Kaufpreis bearbeiten\n"
                        f"❌ Kaufpreis löschen\n"
                        f"👁️ Profit/Loss anzeigen",
            color=0xffd700
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label="ℹ️ Info", style=discord.ButtonStyle.secondary, row=0)
    async def show_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show dashboard information - public action"""
        try:
            summary = self.bot.user_dashboard_service.get_dashboard_summary(self.dashboard_name, self.owner_id)
            if not summary:
                await interaction.response.send_message("❌ Dashboard nicht gefunden.", ephemeral=True)
                return
            
            dashboard = summary['dashboard']
            portfolio = summary['portfolio']
            
            embed = discord.Embed(
                title=f"ℹ️ {dashboard.name}",
                color=0x0099ff,
                timestamp=datetime.now()
            )
            
            # Basic info
            embed.add_field(
                name="� Grunddaten",
                value=(
                    f"**Besitzer:** <@{dashboard.owner_id}>\n"
                    f"**Erstellt:** {dashboard.created_at[:10]}\n"
                    f"**Spieler:** {len(dashboard.player_urls)}\n"
                    f"**Status:** {'🟢 Aktiv' if dashboard.is_active else '🔴 Inaktiv'}"
                ),
                inline=True
            )
            
            # Portfolio stats
            if portfolio:
                total_value = portfolio.get('total_value', 0)
                avg_change = portfolio.get('avg_change', 0)
                
                embed.add_field(
                    name="💰 Portfolio",
                    value=(
                        f"**Gesamtwert:** {total_value:,} Coins\n"
                        f"**Durchschnitt:** {avg_change:+.1f}%\n"
                        f"**Performance:** {'📈' if avg_change >= 0 else '📉'}"
                    ),
                    inline=True
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error showing dashboard info: {e}")
            await interaction.response.send_message("❌ Fehler beim Anzeigen der Dashboard-Informationen.", ephemeral=True)
    
    @discord.ui.button(label="🗑️ Löschen", style=discord.ButtonStyle.danger, row=0)
    async def delete_dashboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Delete dashboard - owner only"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"❌ Nur <@{self.owner_id}> kann dieses Dashboard löschen.", 
                ephemeral=True
            )
            return
        
        # Show confirmation
        view = DeleteDashboardConfirmView(self.bot, self.dashboard_name, self.owner_id)
        embed = discord.Embed(
            title="⚠️ Dashboard löschen?",
            description=f"Möchtest du das Dashboard **{self.dashboard_name}** wirklich löschen?\n\n"
                       f"⚠️ **Diese Aktion kann nicht rückgängig gemacht werden!**",
            color=0xff9900
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AddPlayerToDashboardModal(discord.ui.Modal, title="Spieler zum Dashboard hinzufügen"):
    """Modal for adding players to a dashboard"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__()
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
    
    player_input = discord.ui.TextInput(
        label="Spielernamen (getrennt durch Komma)",
        placeholder="z.B. Messi, Ronaldo, Mbappé",
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            player_names = [name.strip() for name in self.player_input.value.split(',') if name.strip()]
            
            if not player_names:
                await interaction.response.send_message("❌ Keine gültigen Spielernamen eingegeben.", ephemeral=True)
                return
            
            # Add players to dashboard
            added, not_found = self.bot.user_dashboard_service.add_players_to_dashboard(
                self.dashboard_name, self.owner_id, player_names
            )
            
            # Build response
            response_parts = []
            if added:
                response_parts.append(f"✅ {len(added)} Spieler hinzugefügt: {', '.join(added)}")
            
            if not_found:
                response_parts.append(f"❌ {len(not_found)} Spieler nicht gefunden: {', '.join(not_found[:3])}{'...' if len(not_found) > 3 else ''}")
            
            if not added and not not_found:
                response_parts.append("⚠️ Keine Änderungen vorgenommen.")
            
            # Save changes
            if added:
                self.bot.state_service.save_dirty_dashboards()
                
                # Trigger dashboard update
                await self.bot.task_orchestrator.trigger_user_dashboard_update(
                    self.dashboard_name, self.owner_id
                )
            
            await interaction.response.send_message('\n'.join(response_parts), ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error adding players to dashboard: {e}")
            await interaction.response.send_message("❌ Fehler beim Hinzufügen der Spieler.", ephemeral=True)


class RemovePlayerFromDashboardView(discord.ui.View):
    """View for removing players from a dashboard"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
        
        # Get dashboard players for dropdown
        dashboard = bot.state_service.get_user_dashboard(dashboard_name, owner_id)
        if dashboard and dashboard.player_urls:
            options = []
            for url in dashboard.player_urls[:25]:  # Discord limit
                player = bot.state_service.get_player_by_url(url)
                if player:
                    options.append(discord.SelectOption(
                        label=player.name[:100],
                        value=url,
                        description=f"Kategorie: {getattr(player, 'category', 'Allgemein')}"
                    ))
            
            if options:
                self.player_select = discord.ui.Select(
                    placeholder="Wähle Spieler zum Entfernen...",
                    options=options,
                    max_values=min(len(options), 10)  # Allow multiple selections
                )
                self.player_select.callback = self.on_player_select
                self.add_item(self.player_select)
    
    async def on_player_select(self, interaction: discord.Interaction):
        """Handle player selection"""
        try:
            selected_urls = interaction.data['values']
            removed_players = []
            
            for url in selected_urls:
                success = self.bot.state_service.remove_player_from_dashboard(
                    self.dashboard_name, self.owner_id, url
                )
                if success:
                    player = self.bot.state_service.get_player_by_url(url)
                    if player:
                        removed_players.append(player.name)
            
            if removed_players:
                self.bot.state_service.save_dirty_dashboards()
                
                # Trigger dashboard update
                await self.bot.task_orchestrator.trigger_user_dashboard_update(
                    self.dashboard_name, self.owner_id
                )
                
                await interaction.response.send_message(
                    f"✅ {len(removed_players)} Spieler entfernt: {', '.join(removed_players)}", 
                    ephemeral=True
                )
            else:
                await interaction.response.send_message("❌ Keine Spieler wurden entfernt.", ephemeral=True)
                
        except Exception as e:
            logging.error(f"Error removing players from dashboard: {e}")
            await interaction.response.send_message("❌ Fehler beim Entfernen der Spieler.", ephemeral=True)


class DashboardAlertManagementView(discord.ui.View):
    """View for managing alerts for dashboard players"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
        
        # Get dashboard players for dropdown
        dashboard = bot.state_service.get_user_dashboard(dashboard_name, owner_id)
        if dashboard and dashboard.player_urls:
            options = []
            for url in dashboard.player_urls[:25]:  # Discord limit
                player = bot.state_service.get_player_by_url(url)
                if player:
                    # Show current alert status
                    alert_status = ""
                    if getattr(player, 'alert_above', None):
                        alert_status += f"↗️{player.alert_above}"
                    if getattr(player, 'alert_below', None):
                        alert_status += f"↘️{player.alert_below}" 
                    if not alert_status:
                        alert_status = "Keine Alerts"
                    
                    options.append(discord.SelectOption(
                        label=player.name[:90],
                        value=url,
                        description=alert_status[:100]
                    ))
            
            if options:
                self.player_select = discord.ui.Select(
                    placeholder="Wähle Spieler für Alert-Verwaltung...",
                    options=options
                )
                self.player_select.callback = self.on_player_select
                self.add_item(self.player_select)
    
    async def on_player_select(self, interaction: discord.Interaction):
        """Handle player selection for alert management"""
        try:
            selected_url = interaction.data['values'][0]
            player = self.bot.state_service.get_player_by_url(selected_url)
            
            if not player:
                await interaction.response.send_message("❌ Spieler nicht gefunden.", ephemeral=True)
                return
            
            # Show alert management for selected player
            view = AlertManagementView(self.bot, player.name, interaction.user.id)
            embed = discord.Embed(
                title=f"🔔 Benachrichtigungen: {player.name}",
                description="Verwalte Preis-Benachrichtigungen für diesen Spieler:",
                color=0x0099ff
            )
            
            # Show current alerts
            current_alerts = []
            if getattr(player, 'alert_above', None):
                current_alerts.append(f"📈 **Über:** {player.alert_above:,} Coins")
            if getattr(player, 'alert_below', None):
                current_alerts.append(f"📉 **Unter:** {player.alert_below:,} Coins")
            
            if current_alerts:
                embed.add_field(
                    name="🔔 Aktuelle Benachrichtigungen",
                    value="\n".join(current_alerts),
                    inline=False
                )
            else:
                embed.add_field(
                    name="🔕 Keine aktiven Benachrichtigungen",
                    value="Erstelle neue Benachrichtigungen mit den Buttons unten.",
                    inline=False
                )
            
            # Show current price
            state = self.bot.state_service.get_state_by_url(selected_url)
            if state and state.price:
                embed.add_field(
                    name="💰 Aktueller Preis",
                    value=f"{state.price:,} Coins",
                    inline=True
                )
            
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error in alert management: {e}")
            await interaction.response.send_message("❌ Fehler bei der Alert-Verwaltung.", ephemeral=True)


class DeleteDashboardConfirmView(discord.ui.View):
    """Confirmation view for dashboard deletion"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
    
    @discord.ui.button(label="✅ Ja, löschen", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm dashboard deletion"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Du kannst nur deine eigenen Dashboards löschen.", ephemeral=True)
            return
        
        try:
            # Delete the dashboard message from the user dashboard channel first
            await self.bot._delete_user_dashboard_message(self.dashboard_name, self.owner_id)
            
            # Delete the dashboard itself
            success = self.bot.state_service.delete_user_dashboard(self.dashboard_name, self.owner_id)
            
            if success:
                # Save changes
                self.bot.state_service.save_dirty_dashboards()
                
                embed = discord.Embed(
                    title="✅ Dashboard gelöscht",
                    description=f"Das Dashboard **{self.dashboard_name}** wurde erfolgreich gelöscht.",
                    color=0x00ff00
                )
                
                # Disable all buttons
                for item in self.children:
                    item.disabled = True
                
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.send_message("❌ Fehler beim Löschen des Dashboards.", ephemeral=True)
        
        except Exception as e:
            logging.error(f"Error deleting dashboard: {e}")
            await interaction.response.send_message("❌ Fehler beim Löschen des Dashboards.", ephemeral=True)
    
    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel dashboard deletion"""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Zugriff verweigert.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="❌ Löschung abgebrochen",
            description=f"Das Dashboard **{self.dashboard_name}** wurde nicht gelöscht.",
            color=0x999999
        )
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(embed=embed, view=self)


# ==============================================
# USER DASHBOARD COMMANDS
# ==============================================

@bot.tree.command(name="create_dashboard", description="Erstelle ein persönliches Dashboard")
@discord.app_commands.describe(
    name="Name für dein Dashboard (max. 50 Zeichen)",
    players="Optional: Spielernamen, getrennt durch Komma"
)
async def create_dashboard(interaction: discord.Interaction, name: str, players: str = None):
    """Create a personal user dashboard"""
    try:
        # Defer interaction to prevent timeout during dashboard creation
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        
        # Parse players if provided
        initial_players = []
        if players:
            initial_players = [p.strip() for p in players.split(',') if p.strip()]
        
        # Create dashboard
        success, message = bot.user_dashboard_service.create_dashboard_interactive(
            owner_id=user_id,
            name=name,
            initial_players=initial_players
        )
        
        if success:
            # Save changes
            bot.state_service.save_dirty_dashboards()
            
            # Show the new dashboard
            embed = bot.user_dashboard_service.format_dashboard_embed(name, user_id)
            if embed:
                await interaction.followup.send(message, embed=embed)
            else:
                await interaction.followup.send(message)
        else:
            await interaction.followup.send(f"❌ {message}")
    
    except Exception as e:
        logging.error(f"Error creating dashboard: {e}")
        try:
            await interaction.followup.send(
                "❌ Fehler beim Erstellen des Dashboards. Bitte versuche es später erneut.",
                ephemeral=True
            )
        except Exception as followup_error:
            logging.error(f"Error in followup: {followup_error}")
@bot.tree.command(name="my_dashboards", description="Zeige deine persönlichen Dashboards")
async def my_dashboards(interaction: discord.Interaction):
    """List user's personal dashboards"""
    try:
        # Defer interaction to prevent timeout during dashboard analysis
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        dashboards = bot.user_dashboard_service.get_user_dashboard_list(user_id)
        
        if not dashboards:
            await interaction.followup.send(
                "📋 Du hast noch keine Dashboards erstellt.\n"
                "Verwende `/create_dashboard` um ein neues Dashboard zu erstellen."
            )
            return
        
        # Create overview embed
        embed = discord.Embed(
            title="📊 Deine Dashboards",
            description=f"Du hast {len(dashboards)} Dashboard{'s' if len(dashboards) != 1 else ''}",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        
        for dashboard in dashboards[:10]:  # Limit to 10 dashboards
            total_value = dashboard['total_value']
            avg_change = dashboard['avg_change']
            player_count = dashboard['player_count']
            
            # Format values
            value_str = f"{total_value:,} Coins" if total_value > 0 else "Keine Daten"
            change_str = f"{avg_change:+.1f}%" if avg_change != 0 else "±0.0%"
            change_emoji = "📈" if avg_change > 0 else "📉" if avg_change < 0 else "➡️"
            
            embed.add_field(
                name=f"{change_emoji} {dashboard['name']}",
                value=(
                    f"**Wert:** {value_str}\n"
                    f"**Änderung:** {change_str}\n"
                    f"**Spieler:** {player_count}"
                ),
                inline=True
            )
        
        embed.set_footer(text="Verwende /show_dashboard [name] für Details")
        await interaction.followup.send(embed=embed)
    
    except Exception as e:
        logging.error(f"Error listing dashboards: {e}")
        try:
            await interaction.followup.send(
                "❌ Fehler beim Laden der Dashboards. Bitte versuche es später erneut."
            )
        except Exception as followup_error:
            logging.error(f"Error in followup: {followup_error}")


@bot.tree.command(name="show_dashboard", description="Zeige ein spezifisches Dashboard öffentlich")
@discord.app_commands.describe(
    name="Name des Dashboards",
    owner="Besitzer des Dashboards (optional, falls nicht dein eigenes)"
)
async def show_dashboard(interaction: discord.Interaction, name: str, owner: discord.Member = None):
    """Show a specific user dashboard - posts in user dashboard channel like main dashboard"""
    try:
        # Defer the interaction immediately to avoid timeout
        await interaction.response.defer(ephemeral=True)
        
        # Determine owner
        if owner:
            owner_id = owner.id
        else:
            owner_id = interaction.user.id
        
        # Check if user dashboard channel is configured
        if not bot.user_dashboard_channel:
            await interaction.followup.send(
                "❌ User Dashboard Channel ist nicht konfiguriert.\n"
                "Ein Admin muss `/set_user_dashboard_channel` verwenden.",
                ephemeral=True
            )
            return
        
        # Check if dashboard exists
        dashboard = bot.state_service.get_user_dashboard(name, owner_id)
        if not dashboard:
            # Try to find dashboard by searching all users
            found_dashboard = None
            found_owner_id = None
            
            for dashboard_key, dash in bot.state_service.user_dashboards.items():
                if dash.name.lower() == name.lower():
                    found_dashboard = dash
                    found_owner_id = dash.owner_id
                    break
            
            if found_dashboard:
                dashboard = found_dashboard
                owner_id = found_owner_id
            else:
                await interaction.followup.send(
                    f"❌ Dashboard '{name}' nicht gefunden. Verwende `/my_dashboards` um deine Dashboards zu sehen.",
                    ephemeral=True
                )
                return
        
        # WICHTIG: Immer im User Dashboard Channel posten, NICHT als Antwort
        # Dies ist identisch zur Main Dashboard Logik
        try:
            await bot._post_user_dashboard_message(name, owner_id, force_new=True)
            
            # Erfolgreiche Bestätigung als ephemeral message
            channel_mention = bot.user_dashboard_channel.mention
            await interaction.followup.send(
                f"✅ Dashboard **{name}** wurde in {channel_mention} gepostet/aktualisiert!",
                ephemeral=True
            )
            
        except Exception as post_error:
            logging.error(f"Error posting dashboard {name}: {post_error}")
            await interaction.followup.send(
                f"❌ Fehler beim Posten des Dashboards: {str(post_error)[:100]}",
                ephemeral=True
            )
    
    except Exception as e:
        logging.error(f"Error showing dashboard: {e}")
        # Since we deferred, always use followup
        try:
            await interaction.followup.send(
                "❌ Fehler beim Anzeigen des Dashboards. Bitte versuche es später erneut.",
                ephemeral=True
            )
        except Exception as followup_error:
            logging.error(f"Error in followup: {followup_error}")


@bot.tree.command(name="delete_dashboard", description="Lösche ein persönliches Dashboard")
@discord.app_commands.describe(name="Name des zu löschenden Dashboards")
async def delete_dashboard(interaction: discord.Interaction, name: str):
    """Delete a personal user dashboard"""
    try:
        user_id = interaction.user.id
        
        # Check if dashboard exists
        if not bot.user_dashboard_service.validate_dashboard_ownership(name, user_id):
            await interaction.response.send_message(
                f"❌ Dashboard '{name}' nicht gefunden oder du bist nicht der Besitzer.",
                ephemeral=True
            )
            return
        
        # Create confirmation view
        view = DeleteDashboardConfirmView(bot, name, user_id)
        embed = discord.Embed(
            title="⚠️ Dashboard löschen?",
            description=f"Möchtest du das Dashboard **{name}** wirklich löschen?\n\n"
                       f"⚠️ **Diese Aktion kann nicht rückgängig gemacht werden!**",
            color=0xff9900
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    except Exception as e:
        logging.error(f"Error deleting dashboard: {e}")
        await interaction.response.send_message(
            "❌ Fehler beim Löschen des Dashboards. Bitte versuche es später erneut.",
            ephemeral=True
        )


@bot.tree.command(name="import_squad", description="Importiere ein Squad von FUTBin und erstelle ein Dashboard")
@discord.app_commands.describe(
    squad_url="FUTBin Squad URL (z.B. https://www.futbin.com/26/squad/285792)",
    dashboard_name="Optionaler Name für das Dashboard (Standard: aus Squad-Seite)"
)
async def import_squad(interaction: discord.Interaction, squad_url: str, dashboard_name: str = None):
    """Import a squad from FUTBin and create a user dashboard"""
    try:
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        
        # Validate URL format
        squad_pattern = r'https?://(?:www\.)?futbin\.com/(\d+)/squad/(\d+)'
        match = re.match(squad_pattern, squad_url.strip())
        
        if not match:
            await interaction.followup.send(
                "❌ Ungültige URL! Bitte verwende eine gültige FUTBin Squad URL.\n"
                "Format: `https://www.futbin.com/27/squad/XXXXXX`",
                ephemeral=True
            )
            return
        
        squad_year = match.group(1)
        squad_id = match.group(2)
        logging.info(f"Importing squad {squad_id} (year {squad_year}) for user {user_id}")
        
        # Fetch squad page
        try:
            response = await bot.http_client.get(squad_url)
            if response.status != 200:
                await interaction.followup.send(
                    f"❌ Squad konnte nicht geladen werden (HTTP {response.status})",
                    ephemeral=True
                )
                return
            
            html_content = await response.text()
        except Exception as e:
            logging.error(f"Error fetching squad page: {e}")
            await interaction.followup.send(
                "❌ Fehler beim Laden der Squad-Seite. Bitte versuche es später erneut.",
                ephemeral=True
            )
            return
        
        # Parse HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Extract squad name from page header
        extracted_name = None
        if not dashboard_name:
            header = soup.find('h1', class_='page-header-top')
            if header:
                full_title = header.get_text(strip=True)
                # Extract name before |
                if '|' in full_title:
                    extracted_name = full_title.split('|')[0].strip()
                else:
                    extracted_name = full_title.strip()
                
                # Clean up name
                extracted_name = extracted_name[:50]  # Limit length
        
        final_dashboard_name = dashboard_name or extracted_name or f"Squad {squad_id}"
        
        # Check if name exists and increment if needed
        original_name = final_dashboard_name
        counter = 1
        while bot.state_service.get_user_dashboard(final_dashboard_name, user_id):
            final_dashboard_name = f"{original_name}_{counter}"
            counter += 1
        
        logging.info(f"Dashboard name: {final_dashboard_name}")
        
        # Extract player data from squadData JSON in script tags
        player_ids = []
        skipped_players = []
        
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string and 'squadData' in script.string:
                try:
                    # Extract JSON from script
                    script_content = script.string.strip()
                    
                    # Find where squadData starts
                    start_idx = script_content.find('{"squadData":')
                    if start_idx == -1:
                        continue
                    
                    # Count braces to find the matching closing brace
                    brace_count = 0
                    end_idx = start_idx
                    
                    for i in range(start_idx, len(script_content)):
                        char = script_content[i]
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                end_idx = i + 1
                                break
                    
                    if end_idx > start_idx:
                        json_str = script_content[start_idx:end_idx]
                        data = json.loads(json_str)
                        
                        squad = data.get('squadData', {}).get('squad', [])
                        logging.info(f"Found {len(squad)} squad entries in JSON")
                        
                        # Import RARE_TYPE_MAPPING from price_service
                        from services.price_service import RARE_TYPE_MAPPING
                        
                        for entry in squad:
                            # Check if it's a player entry (has 'id' field)
                            if isinstance(entry, dict) and 'id' in entry:
                                player_card_id = entry.get('id', {}).get('playerCardId', {}).get('value')
                                player_name = entry.get('playerName', 'Unknown')
                                position = entry.get('position', {}).get('value', 'N/A')
                                rare_type = entry.get('rareType', 1)  # Default to 1 (Gold)
                                
                                # Get card version from rare type
                                card_version = RARE_TYPE_MAPPING.get(rare_type, "")
                                
                                if player_card_id:
                                    player_ids.append({
                                        'id': player_card_id,
                                        'name': player_name,
                                        'position': position,
                                        'card_version': card_version
                                    })
                                    logging.debug(f"Found player: {player_name} ({position}) [{card_version or 'Gold'}] - ID: {player_card_id}")
                        
                        break  # Found squadData, no need to check other scripts
                        
                except json.JSONDecodeError as e:
                    logging.error(f"Error parsing squadData JSON: {e}")
                    continue
                except Exception as e:
                    logging.error(f"Error extracting player data: {e}")
                    continue
        
        if not player_ids:
            await interaction.followup.send(
                "❌ Keine Spieler im Squad gefunden. Bitte überprüfe die URL.",
                ephemeral=True
            )
            return
        
        logging.info(f"Found {len(player_ids)} players in squad")
        
        # Helper function to create FUTBin player slug from name
        import unicodedata
        def create_player_slug(name):
            """Create FUTBin-style player slug from name"""
            # Remove accents/diacritics
            name = unicodedata.normalize('NFD', name)
            name = ''.join(char for char in name if unicodedata.category(char) != 'Mn')
            
            # Convert to lowercase
            name = name.lower()
            
            # Replace spaces with hyphens
            name = name.replace(' ', '-')
            
            # Remove special characters (keep only letters, numbers, hyphens)
            name = re.sub(r'[^a-z0-9\-]', '', name)
            
            # Remove multiple consecutive hyphens
            name = re.sub(r'-+', '-', name)
            
            # Remove leading/trailing hyphens
            name = name.strip('-')
            
            return name
        
        # Convert player IDs to FUTBin URLs with name slugs
        player_urls = []
        player_card_versions = {}  # url -> card_version mapping
        
        for player_data in player_ids:
            player_id = player_data['id']
            player_name = player_data['name']
            player_slug = create_player_slug(player_name)
            year_to_use = squad_year or "27"
            player_url = f"https://www.futbin.com/{year_to_use}/player/{player_id}/{player_slug}"
            player_urls.append(player_url)
            player_card_versions[player_url] = player_data.get('card_version', '')
        
        # Remove duplicates while preserving order
        seen = set()
        unique_player_urls = []
        for url in player_urls:
            if url not in seen:
                seen.add(url)
                unique_player_urls.append(url)
        
        player_urls = unique_player_urls
        logging.info(f"Generated {len(player_urls)} unique player URLs")
        
        # Update progress
        progress_embed = discord.Embed(
            title="⏳ Importiere Squad...",
            description=f"**Squad:** {final_dashboard_name}\n"
                       f"**Spieler gefunden:** {len(player_urls)}\n\n"
                       f"Füge Spieler zum Main Dashboard hinzu...",
            color=0x00ff00
        )
        progress_message = await interaction.followup.send(embed=progress_embed, ephemeral=True)
        
        # Add players to main dashboard if not exists
        added_to_main = 0
        existing_in_main = 0
        
        # Create a mapping of URLs to player data for easy lookup
        url_to_player_data = {}
        for player_data in player_ids:
            player_slug = create_player_slug(player_data['name'])
            year_to_use = squad_year or "27"
            player_url = f"https://www.futbin.com/{year_to_use}/player/{player_data['id']}/{player_slug}"
            url_to_player_data[player_url] = player_data
        
        for url in player_urls:
            # Get player data from our mapping
            player_data = url_to_player_data.get(url)
            
            if player_data:
                player_name = player_data['name']
                card_version = player_data.get('card_version', '')
                
                try:
                    # Try to add to main dashboard
                    # add_player() will check for duplicates using normalized URLs
                    from services import PlayerConfig
                    new_player = PlayerConfig(
                        name=player_name,
                        url=url,
                        active=True,
                        category=f"Squad {squad_id}",
                        card_version=card_version
                    )
                    
                    # add_player returns False if player already exists (including normalized URL matches)
                    if bot.state_service.add_player(new_player):
                        added_to_main += 1
                        logging.info(f"Added new player to main dashboard: {player_name} [{card_version or 'Gold'}]")
                    else:
                        existing_in_main += 1
                        logging.debug(f"Player already exists (normalized URL match): {player_name}")
                    
                except Exception as e:
                    logging.error(f"Error adding player {url}: {e}")
                    skipped_players.append(f"{player_name} (Fehler)")
                    continue
            else:
                skipped_players.append(f"Player {url.split('/')[-1]} (Keine Daten)")
        
        # Save changes to main dashboard
        bot.state_service.save_all()
        
        # Update progress
        progress_embed.description = (
            f"**Squad:** {final_dashboard_name}\n"
            f"**Spieler gefunden:** {len(player_urls)}\n"
            f"**Neu hinzugefügt:** {added_to_main}\n"
            f"**Bereits vorhanden:** {existing_in_main}\n\n"
            f"Erstelle User Dashboard..."
        )
        await progress_message.edit(embed=progress_embed)
        
        # Create user dashboard
        success = bot.state_service.create_user_dashboard(
            name=final_dashboard_name,
            owner_id=user_id,
            player_urls=player_urls,
            notes=f"Importiert von FUTBin Squad {squad_id}"
        )
        
        if not success:
            await interaction.followup.send(
                f"❌ Fehler beim Erstellen des Dashboards '{final_dashboard_name}'.",
                ephemeral=True
            )
            return
        
        bot.state_service.save_all()
        
        # Post dashboard in user dashboard channel
        if bot.user_dashboard_channel:
            await bot._post_user_dashboard_message(final_dashboard_name, user_id, force_new=True)
        
        # Create success message
        success_embed = discord.Embed(
            title="✅ Squad erfolgreich importiert!",
            description=(
                f"**Dashboard Name:** {final_dashboard_name}\n"
                f"**Spieler importiert:** {len(player_urls)}\n"
                f"**Neu zum Main Dashboard:** {added_to_main}\n"
                f"**Bereits vorhanden:** {existing_in_main}"
            ),
            color=0x00ff00
        )
        
        if skipped_players:
            success_embed.add_field(
                name="⚠️ Übersprungene Spieler",
                value="\n".join(skipped_players[:10]),  # Limit to 10
                inline=False
            )
        
        if bot.user_dashboard_channel:
            success_embed.add_field(
                name="📊 Dashboard",
                value=f"Wurde in {bot.user_dashboard_channel.mention} gepostet!",
                inline=False
            )
        
        success_embed.set_footer(text=f"Squad ID: {squad_id}")
        
        await progress_message.edit(embed=success_embed)
        
        logging.info(f"Squad import completed: {final_dashboard_name} with {len(player_urls)} players")
        
    except Exception as e:
        logging.error(f"Error importing squad: {e}", exc_info=True)
        try:
            await interaction.followup.send(
                "❌ Fehler beim Importieren des Squads. Bitte versuche es später erneut.",
                ephemeral=True
            )
        except:
            pass


@bot.tree.command(name="add_to_dashboard", description="Füge Spieler zu einem Dashboard hinzu")
@discord.app_commands.describe(
    dashboard="Name des Dashboards",
    players="Spielernamen, getrennt durch Komma"
)
async def add_to_dashboard(interaction: discord.Interaction, dashboard: str, players: str):
    """Add players to a user dashboard"""
    try:
        # Defer interaction to prevent timeout during player processing
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        
        # Check if dashboard exists and user owns it
        if not bot.user_dashboard_service.validate_dashboard_ownership(dashboard, user_id):
            await interaction.followup.send(
                f"❌ Dashboard '{dashboard}' nicht gefunden oder du bist nicht der Besitzer."
            )
            return
        
        # Parse players
        player_names = [p.strip() for p in players.split(',') if p.strip()]
        if not player_names:
            await interaction.followup.send("❌ Keine gültigen Spielernamen angegeben.")
            return
        
        # Add players to dashboard
        added, not_found = bot.user_dashboard_service.add_players_to_dashboard(
            dashboard, user_id, player_names
        )
        
        # Build response message
        response_parts = []
        if added:
            response_parts.append(f"✅ {len(added)} Spieler hinzugefügt: {', '.join(added)}")
        
        if not_found:
            response_parts.append(f"❌ {len(not_found)} Spieler nicht gefunden: {', '.join(not_found[:3])}{'...' if len(not_found) > 3 else ''}")
        
        if not added:
            response_parts.append("⚠️ Keine Spieler wurden hinzugefügt.")
        
        # Save changes
        if added:
            bot.state_service.save_dirty_dashboards()
            
            # Trigger dashboard update
            await bot.task_orchestrator.trigger_user_dashboard_update(dashboard, user_id)
        
        await interaction.followup.send('\n'.join(response_parts))
    
    except Exception as e:
        logging.error(f"Error adding players to dashboard: {e}")
        try:
            await interaction.followup.send(
                "❌ Fehler beim Hinzufügen der Spieler. Bitte versuche es später erneut."
            )
        except Exception as followup_error:
            logging.error(f"Error in followup: {followup_error}")


@bot.tree.command(name="remove_from_dashboard", description="Entferne Spieler von einem Dashboard")
@discord.app_commands.describe(
    dashboard="Name des Dashboards",
    players="Spielernamen, getrennt durch Komma"
)
async def remove_from_dashboard(interaction: discord.Interaction, dashboard: str, players: str):
    """Remove players from a user dashboard"""
    try:
        # Defer interaction to prevent timeout during player processing
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        
        # Check if dashboard exists and user owns it
        if not bot.user_dashboard_service.validate_dashboard_ownership(dashboard, user_id):
            await interaction.followup.send(
                f"❌ Dashboard '{dashboard}' nicht gefunden oder du bist nicht der Besitzer."
            )
            return
        
        # Parse players
        player_names = [p.strip() for p in players.split(',') if p.strip()]
        if not player_names:
            await interaction.followup.send("❌ Keine gültigen Spielernamen angegeben.")
            return
        
        # Remove players from dashboard
        removed, not_found = bot.user_dashboard_service.remove_players_from_dashboard(
            dashboard, user_id, player_names
        )
        
        # Build response message
        response_parts = []
        if removed:
            response_parts.append(f"✅ {len(removed)} Spieler entfernt: {', '.join(removed)}")
        
        if not_found:
            response_parts.append(f"❌ {len(not_found)} Spieler nicht im Dashboard gefunden: {', '.join(not_found[:3])}{'...' if len(not_found) > 3 else ''}")
        
        if not removed:
            response_parts.append("⚠️ Keine Spieler wurden entfernt.")
        
        # Save changes
        if removed:
            bot.state_service.save_dirty_dashboards()
            
            # Trigger dashboard update
            await bot.task_orchestrator.trigger_user_dashboard_update(dashboard, user_id)
        
        await interaction.followup.send('\n'.join(response_parts))
    
    except Exception as e:
        logging.error(f"Error removing players from dashboard: {e}")
        try:
            await interaction.followup.send(
                "❌ Fehler beim Entfernen der Spieler. Bitte versuche es später erneut."
            )
        except Exception as followup_error:
            logging.error(f"Error in followup: {followup_error}")


@bot.tree.command(name="list_dashboards", description="Zeige alle verfügbaren User-Dashboards")
async def list_all_dashboards(interaction: discord.Interaction):
    """List all available user dashboards"""
    try:
        # Defer interaction to prevent timeout during dashboard processing
        await interaction.response.defer(ephemeral=False)
        
        all_dashboards = []
        
        # Get all dashboards
        for dashboard_key, dashboard in bot.state_service.user_dashboards.items():
            if dashboard.is_active:
                # Get portfolio stats
                portfolio_stats = bot.state_service.get_dashboard_portfolio_stats(dashboard.name, dashboard.owner_id)
                
                all_dashboards.append({
                    'name': dashboard.name,
                    'owner_id': dashboard.owner_id,
                    'player_count': len(dashboard.player_urls),
                    'total_value': portfolio_stats.get('total_value', 0),
                    'avg_change': portfolio_stats.get('avg_change', 0),
                    'created_at': dashboard.created_at,
                    'last_updated': dashboard.last_updated
                })
        
        if not all_dashboards:
            await interaction.followup.send(
                "📋 Keine User-Dashboards verfügbar.\n"
                "Erstelle dein erstes Dashboard mit `/create_dashboard`!"
            )
            return
        
        # Sort by last updated (most recent first)
        all_dashboards.sort(key=lambda x: x['last_updated'], reverse=True)
        
        # Create overview embed
        embed = discord.Embed(
            title="📊 Alle User-Dashboards",
            description=f"Es gibt {len(all_dashboards)} aktive Dashboard{'s' if len(all_dashboards) != 1 else ''}",
            color=0x0099ff,
            timestamp=datetime.now()
        )
        
        for dashboard in all_dashboards[:12]:  # Limit to 12 dashboards
            total_value = dashboard['total_value']
            avg_change = dashboard['avg_change']
            player_count = dashboard['player_count']
            
            # Format values
            value_str = f"{total_value:,} Coins" if total_value > 0 else "Keine Daten"
            change_str = f"{avg_change:+.1f}%" if avg_change != 0 else "±0.0%"
            change_emoji = "📈" if avg_change > 0 else "📉" if avg_change < 0 else "➡️"
            
            embed.add_field(
                name=f"{change_emoji} {dashboard['name']}",
                value=(
                    f"👤 <@{dashboard['owner_id']}>\n"
                    f"💰 {value_str}\n"
                    f"📊 {change_str} • {player_count} Spieler"
                ),
                inline=True
            )
        
        embed.set_footer(text="Verwende /show_dashboard [name] um ein Dashboard anzuzeigen")
        await interaction.followup.send(embed=embed)
    
    except Exception as e:
        logging.error(f"Error listing all dashboards: {e}")
        try:
            await interaction.followup.send(
                "❌ Fehler beim Laden der Dashboards. Bitte versuche es später erneut."
            )
        except Exception as followup_error:
            logging.error(f"Error in followup: {followup_error}")


class PurchasePriceManagementView(discord.ui.View):
    """Interactive view for managing purchase prices in a user dashboard"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
    
    @discord.ui.button(label="💰 Kaufpreis setzen", style=discord.ButtonStyle.success, row=0)
    async def set_purchase_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Set purchase price for a player"""
        try:
            dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
            if not dashboard or not dashboard.player_urls:
                await interaction.response.send_message("❌ Keine Spieler in diesem Dashboard.", ephemeral=True)
                return
            
            # Show select menu for players without purchase prices
            view = SetPurchasePricePlayerSelectView(self.bot, self.dashboard_name, self.owner_id)
            await interaction.response.send_message(
                "💰 **Kaufpreis setzen**\n\nWähle einen Spieler aus:", 
                view=view, 
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Error in set_purchase_price: {e}")
            await safe_interaction_response(
                interaction,
                content="❌ Fehler beim Laden der Spielerliste. Bitte versuche es später erneut.",
                ephemeral=True
            )
    
    @discord.ui.button(label="✏️ Kaufpreis bearbeiten", style=discord.ButtonStyle.primary, row=0)
    async def edit_purchase_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Edit existing purchase price for a player"""
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard or not dashboard.purchase_prices:
            await interaction.response.send_message("❌ Keine Kaufpreise in diesem Dashboard gesetzt.", ephemeral=True)
            return
        
        # Show select menu for players with purchase prices
        view = EditPurchasePricePlayerSelectView(self.bot, self.dashboard_name, self.owner_id)
        await interaction.response.send_message(
            "✏️ **Kaufpreis bearbeiten**\n\nWähle einen Spieler aus:", 
            view=view, 
            ephemeral=True
        )
    
    @discord.ui.button(label="❌ Kaufpreis löschen", style=discord.ButtonStyle.danger, row=0)
    async def remove_purchase_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Remove purchase price for a player"""
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard or not dashboard.purchase_prices:
            await interaction.response.send_message("❌ Keine Kaufpreise in diesem Dashboard gesetzt.", ephemeral=True)
            return
        
        # Show select menu for players with purchase prices
        view = RemovePurchasePricePlayerSelectView(self.bot, self.dashboard_name, self.owner_id)
        await interaction.response.send_message(
            "❌ **Kaufpreis löschen**\n\nWähle einen Spieler aus:", 
            view=view, 
            ephemeral=True
        )
    
    @discord.ui.button(label="📊 P/L anzeigen", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_profit_loss_display(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle profit/loss display in dashboard"""
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard:
            await interaction.response.send_message("❌ Dashboard nicht gefunden.", ephemeral=True)
            return
        
        # Toggle the show_purchase_prices flag
        dashboard.show_purchase_prices = not dashboard.show_purchase_prices
        self.bot.state_service._dirty_dashboards.add(f"{self.owner_id}_{self.dashboard_name.lower().replace(' ', '_')}")
        self.bot.state_service.save_all()
        
        status = "aktiviert" if dashboard.show_purchase_prices else "deaktiviert"
        emoji = "👁️" if dashboard.show_purchase_prices else "🙈"
        
        await interaction.response.send_message(
            f"{emoji} **Profit/Loss-Anzeige {status}**\n\n"
            f"Das Dashboard zeigt {'jetzt' if dashboard.show_purchase_prices else 'nicht mehr'} Kaufpreise und Profit/Loss an.\n"
            f"Aktualisiere das Dashboard um die Änderungen zu sehen.",
            ephemeral=True
        )


class SetPurchasePricePlayerSelectView(discord.ui.View):
    """Select view for choosing a player to set purchase price for"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
        
        # Add select menu with players
        self.add_player_select()
    
    def add_player_select(self):
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard:
            return
        
        options = []
        for player_url in dashboard.player_urls[:25]:  # Discord limit
            player = self.bot.state_service.get_player_by_url(player_url)
            if player:
                # Show current price in option
                state = self.bot.state_service.get_state_by_url(player_url)
                current_price = f" (aktuell: {state.price:,})" if state and state.price else ""
                
                options.append(discord.SelectOption(
                    label=player.name[:50], 
                    value=player_url,
                    description=f"Kaufpreis setzen{current_price}",
                    emoji="💰"
                ))
        
        if options:
            select = discord.ui.Select(
                placeholder="Spieler auswählen...",
                options=options,
                custom_id="select_player_for_purchase_price"
            )
            select.callback = self.player_selected
            self.add_item(select)
    
    async def player_selected(self, interaction: discord.Interaction):
        player_url = interaction.data['values'][0]
        player = self.bot.state_service.get_player_by_url(player_url)
        
        if not player:
            await interaction.response.send_message("❌ Spieler nicht gefunden.", ephemeral=True)
            return
        
        # Show purchase price input modal
        modal = SetPurchasePriceModal(self.bot, self.dashboard_name, self.owner_id, player_url, player.name)
        await interaction.response.send_modal(modal)


class EditPurchasePricePlayerSelectView(discord.ui.View):
    """Select view for choosing a player to edit purchase price for"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
        
        # Add select menu with players that have purchase prices
        self.add_player_select()
    
    def add_player_select(self):
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard:
            return
        
        options = []
        for player_url, purchase_price in list(dashboard.purchase_prices.items())[:25]:  # Discord limit
            player = self.bot.state_service.get_player_by_url(player_url)
            if player:
                options.append(discord.SelectOption(
                    label=player.name[:50], 
                    value=player_url,
                    description=f"Gekauft für: {purchase_price:,} Coins",
                    emoji="✏️"
                ))
        
        if options:
            select = discord.ui.Select(
                placeholder="Spieler mit Kaufpreis auswählen...",
                options=options,
                custom_id="select_player_for_edit_purchase_price"
            )
            select.callback = self.player_selected
            self.add_item(select)
    
    async def player_selected(self, interaction: discord.Interaction):
        player_url = interaction.data['values'][0]
        player = self.bot.state_service.get_player_by_url(player_url)
        
        if not player:
            await interaction.response.send_message("❌ Spieler nicht gefunden.", ephemeral=True)
            return
        
        # Show purchase price edit modal with current value
        current_price = self.bot.state_service.get_purchase_price(self.dashboard_name, self.owner_id, player_url)
        modal = SetPurchasePriceModal(self.bot, self.dashboard_name, self.owner_id, player_url, player.name, current_price)
        await interaction.response.send_modal(modal)


class RemovePurchasePricePlayerSelectView(discord.ui.View):
    """Select view for choosing a player to remove purchase price for"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
        
        # Add select menu with players that have purchase prices
        self.add_player_select()
    
    def add_player_select(self):
        dashboard = self.bot.state_service.get_user_dashboard(self.dashboard_name, self.owner_id)
        if not dashboard:
            return
        
        options = []
        for player_url, purchase_price in list(dashboard.purchase_prices.items())[:25]:  # Discord limit
            player = self.bot.state_service.get_player_by_url(player_url)
            if player:
                options.append(discord.SelectOption(
                    label=player.name[:50], 
                    value=player_url,
                    description=f"Gekauft für: {purchase_price:,} Coins",
                    emoji="❌"
                ))
        
        if options:
            select = discord.ui.Select(
                placeholder="Spieler mit Kaufpreis auswählen...",
                options=options,
                custom_id="select_player_for_remove_purchase_price"
            )
            select.callback = self.player_selected
            self.add_item(select)
    
    async def player_selected(self, interaction: discord.Interaction):
        player_url = interaction.data['values'][0]
        player = self.bot.state_service.get_player_by_url(player_url)
        
        if not player:
            await interaction.response.send_message("❌ Spieler nicht gefunden.", ephemeral=True)
            return
        
        # Remove purchase price
        success = self.bot.state_service.remove_purchase_price(self.dashboard_name, self.owner_id, player_url)
        
        if success:
            self.bot.state_service.save_all()
            
            # Trigger dashboard update
            await self.bot.task_orchestrator.trigger_user_dashboard_update(
                self.dashboard_name, self.owner_id
            )
            
            await interaction.response.send_message(
                f"✅ **Kaufpreis entfernt**\n\n"
                f"Der Kaufpreis für **{player.name}** wurde aus dem Dashboard entfernt.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Fehler beim Entfernen des Kaufpreises.", ephemeral=True)


class SetPurchasePriceModal(discord.ui.Modal):
    """Modal for setting/editing purchase price for a player"""
    
    def __init__(self, bot, dashboard_name: str, owner_id: int, player_url: str, player_name: str, current_price: int = None):
        title = f"Kaufpreis {'bearbeiten' if current_price else 'setzen'} - {player_name[:30]}"
        super().__init__(title=title)
        
        self.bot = bot
        self.dashboard_name = dashboard_name
        self.owner_id = owner_id
        self.player_url = player_url
        self.player_name = player_name
        self.current_price = current_price
        
        # Pre-fill with current price if editing
        default_value = str(current_price) if current_price else ""
        self.purchase_price_input = discord.ui.TextInput(
            label="Kaufpreis (in Coins)",
            placeholder="z.B. 1500000 für 1,5 Millionen Coins",
            default=default_value,
            max_length=12,
            style=discord.TextStyle.short
        )
        self.add_item(self.purchase_price_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Parse price input
            price_str = self.purchase_price_input.value.strip().replace(",", "").replace(".", "")
            
            # Handle common abbreviations
            if price_str.lower().endswith('k'):
                price = int(float(price_str[:-1]) * 1000)
            elif price_str.lower().endswith('m'):
                price = int(float(price_str[:-1]) * 1_000_000)
            else:
                price = int(price_str)
            
            if price <= 0:
                await interaction.response.send_message("❌ Der Kaufpreis muss größer als 0 sein.", ephemeral=True)
                return
            
            if price > 999_999_999:  # 999M limit
                await interaction.response.send_message("❌ Der Kaufpreis darf nicht größer als 999 Millionen sein.", ephemeral=True)
                return
            
            # Set purchase price
            success = self.bot.state_service.set_purchase_price(
                self.dashboard_name, self.owner_id, self.player_url, price
            )
            
            if success:
                self.bot.state_service.save_all()
                
                # Trigger dashboard update
                await self.bot.task_orchestrator.trigger_user_dashboard_update(
                    self.dashboard_name, self.owner_id
                )
                
                # Get current price for comparison
                state = self.bot.state_service.get_state_by_url(self.player_url)
                current_market_price = state.price if state else None
                
                profit_info = ""
                if current_market_price:
                    profit_loss = current_market_price - price
                    if profit_loss > 0:
                        profit_info = f"\n💰 **Aktueller Gewinn:** +{profit_loss:,} Coins"
                    elif profit_loss < 0:
                        profit_info = f"\n📉 **Aktueller Verlust:** {profit_loss:,} Coins"
                    else:
                        profit_info = f"\n➖ **Break Even** (±0 Coins)"
                
                action = "bearbeitet" if self.current_price else "gesetzt"
                await interaction.response.send_message(
                    f"✅ **Kaufpreis {action}**\n\n"
                    f"🎯 **Spieler:** {self.player_name}\n"
                    f"💰 **Kaufpreis:** {price:,} Coins\n"
                    f"📊 **Dashboard:** {self.dashboard_name}"
                    f"{profit_info}",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message("❌ Fehler beim Setzen des Kaufpreises.", ephemeral=True)
                
        except ValueError:
            await interaction.response.send_message(
                "❌ **Ungültiger Kaufpreis**\n\n"
                "Bitte gib einen gültigen Preis ein:\n"
                "• `1500000` für 1,5 Millionen\n"
                "• `1.5M` oder `1500K` funktioniert auch\n"
                "• Keine Buchstaben außer K/M am Ende",
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Error setting purchase price: {e}")
            await interaction.response.send_message("❌ Unerwarteter Fehler beim Setzen des Kaufpreises.", ephemeral=True)


if __name__ == "__main__":
    try:
        # Start health check server in background thread
        if CONFIG is None or CONFIG.health.enabled:
            health_thread = threading.Thread(target=start_health_server, daemon=True)
            health_thread.start()
            logging.info("Health check server started in background")
        
        # Get bot token
        token = CONFIG.discord.bot_token if CONFIG else BOT_TOKEN
        if not token:
            raise RuntimeError("Discord bot token is required")
        
        # Run bot
        bot.run(token)
    except Exception as e:
        logging.error(f"Failed to start bot: {e}")
        sys.exit(1)