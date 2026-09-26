"""
Dashboard service for creating and managing Discord embeds and views.
Provides the ORIGINAL dashboard format from the main bot.
"""

import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any


class DashboardService:
    """Service for managing dashboard creation and updates"""
    
    def __init__(self, state_service):
        self.state_service = state_service
        self.logger = logging.getLogger(__name__)
        
    async def create_dashboard_embed(self):
        """Erstellt das Haupt-Dashboard Embed mit erweiterten Statistiken (ORIGINALES FORMAT)"""
        try:
            embed_dict = {
                'title': "📊 FutBin Live Dashboard",
                'color': 0xfa7100,  # COLOR_BOT
                'timestamp': datetime.now().isoformat()
            }
            
            # Hole alle verfügbaren Spieler aus dem State
            players = self.state_service.get_all_active_players()
            
            if not players:
                embed_dict['description'] = (
                    "❌ Keine Spieler konfiguriert.\n"
                    "Verwende die Buttons unten oder `/add_player` um Spieler hinzuzufügen."
                )
                return embed_dict
            
            # Sammle und analysiere Spielerdaten
            player_data = []
            total_value = 0
            total_min_value = 0
            total_max_value = 0
            available_count = 0
            price_changes_24h = []
            categories = {}
            
            for player in players:
                try:
                    state_data = self.state_service.get_state_by_url(player.url)
                    current_price = state_data.price if state_data else None
                    price_24h_ago = state_data.price_24h_ago if state_data else None
                    min_price = state_data.min_price if state_data and state_data.min_price else (current_price if current_price else 0)
                    max_price = state_data.max_price if state_data and state_data.max_price else (current_price if current_price else 0)
                    
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
                    
                    # Get card version from player config
                    card_version = getattr(player, 'card_version', '')
                    
                    # Spielername mit Event-Tag (wenn vorhanden)
                    base_name = player.name[:13]
                    if card_version:
                        # Add card version suffix (e.g., "Wirtz [TOTW]")
                        display_name = f"{base_name} [{card_version}]"
                        player_name_short = display_name[:20].ljust(20)  # Increase length for event tag
                    else:
                        player_name_short = base_name.ljust(13)
                    
                    # Alert-Indikator (immer 2 Zeichen reserviert)
                    alert_indicator = "🔔" if getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None) else "  "
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
                    
                    # Erstelle Zeile im ORIGINALEN FORMAT
                    line = f"{name_with_alert} {price_str} {h24_str}"
                    
                    player_data.append({
                        'line': line,
                        'price': current_price,
                        'volatility': volatility,
                        'has_alert': bool(getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None))
                    })
                        
                except Exception as e:
                    self.logger.error(f"Fehler beim Verarbeiten von Spieler {getattr(player, 'name', 'Unknown')}: {e}")
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
            
            # Erstelle erweiterte Tabelle im ORIGINALEN FORMAT
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
                avg_volatility = sum(d['volatility'] for d in player_data) / len(player_data) if player_data else 0
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
                
                table_lines.append(f"Spieler: {available_count}/{len(players):<8}")
                
            else:
                table_lines.append("Keine Preisdaten verfügbar")
            
            table_lines.append("```")
            
            embed_dict['description'] = "\n".join(table_lines)
            
            # Zusätzliche Embed-Felder für erweiterte Infos
            if available_count > 0:
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
            platform_name = "PC"
            try:
                from services.config_service import get_config
                cfg = get_config()
                if cfg and getattr(cfg, 'platform', None):
                    platform_name = "PC" if cfg.platform.lower() == "pc" else "PlayStation"
            except Exception:
                pass
            embed_dict['footer'] = {'text': platform_name}
            
            return embed_dict
            
        except Exception as e:
            self.logger.error(f"Kritischer Fehler beim Erstellen des Dashboard-Embeds: {e}")
            # Fallback Embed erstellen
            return {
                'title': "⚠️ Dashboard-Fehler",
                'description': f"Ein Fehler ist beim Laden des Dashboards aufgetreten.\n\n**Fehler**: {str(e)[:100]}...",
                'color': 0xe74c3c,  # COLOR_DOWN
                'timestamp': datetime.now().isoformat()
            }
    
    def clear_cached_data(self) -> None:
        """Clear any cached dashboard data"""
        pass
    
    def get_dashboard_stats(self) -> Dict[str, Any]:
        """Get basic dashboard statistics"""
        return self.state_service.get_portfolio_stats()