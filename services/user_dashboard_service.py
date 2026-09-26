"""
User Dashboard Service for managing individual user dashboards with portfolio tracking.
Provides high-level operations for dashboard creation, management, and analytics.
"""

import logging
import discord
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta

from services.state_service import StateService, UserDashboard, PlayerConfig, PlayerState


class UserDashboardService:
    """High-level service for user dashboard management and portfolio analytics"""
    
    def __init__(self, state_service: StateService):
        self.state_service = state_service
        self._logger = logging.getLogger(__name__)
    
    def _get_platform_name(self) -> str:
        try:
            from services.config_service import get_config
            cfg = get_config()
            if cfg and getattr(cfg, 'platform', None):
                return "PC" if cfg.platform.lower() == "pc" else "PlayStation"
        except Exception:
            pass
        return "PC"
    
    def create_dashboard_interactive(self, owner_id: int, name: str,
                                    initial_players: List[str] = None) -> Tuple[bool, str]:
        """Create a new dashboard with validation and user feedback"""
        # Validate name
        if not name or len(name.strip()) < 2:
            return False, "Dashboard-Name muss mindestens 2 Zeichen lang sein."
        
        if len(name) > 50:
            return False, "Dashboard-Name darf maximal 50 Zeichen lang sein."
        
        # Check if user already has too many dashboards
        existing_dashboards = self.state_service.get_user_dashboards(owner_id)
        if len(existing_dashboards) >= 10:  # Limit per user
            return False, "Du kannst maximal 10 Dashboards erstellen."
        
        # Check for duplicate names
        for dashboard in existing_dashboards:
            if dashboard.name.lower() == name.lower():
                return False, f"Du hast bereits ein Dashboard namens '{name}'."
        
        # Validate initial players
        valid_players = []
        invalid_players = []
        
        if initial_players:
            for player_identifier in initial_players:
                # Try to find player by name or URL
                player = (self.state_service.get_player_by_name(player_identifier) or 
                        self.state_service.get_player_by_url(player_identifier))
                
                if player:
                    valid_players.append(player.url)
                else:
                    invalid_players.append(player_identifier)
        
        # Create dashboard
        success = self.state_service.create_user_dashboard(
            name=name.strip(),
            owner_id=owner_id,
            player_urls=valid_players
        )
        
        if not success:
            return False, "Fehler beim Erstellen des Dashboards."
        
        # Build success message
        message = f"✅ Dashboard '{name}' wurde erfolgreich erstellt!"
        if valid_players:
            message += f"\n📊 {len(valid_players)} Spieler hinzugefügt."
        if invalid_players:
            message += f"\n⚠️ {len(invalid_players)} Spieler nicht gefunden: {', '.join(invalid_players[:3])}{'...' if len(invalid_players) > 3 else ''}"
        
        return True, message
    
    def get_dashboard_summary(self, dashboard_name: str, owner_id: int) -> Optional[Dict[str, Any]]:
        """Get comprehensive dashboard summary with portfolio analytics"""
        dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return None
        
        # Get portfolio stats
        portfolio_stats = self.state_service.get_dashboard_portfolio_stats(dashboard_name, owner_id)
        
        # Get player count and status
        active_players = 0
        total_players = len(dashboard.player_urls)
        error_players = 0
        
        for url in dashboard.player_urls:
            player = self.state_service.get_player_by_url(url)
            state = self.state_service.get_state_by_url(url)
            
            if player and player.active:
                active_players += 1
                if state and state.fetch_errors > 3:
                    error_players += 1
        
        # Calculate time since last update
        last_update = datetime.fromisoformat(dashboard.last_updated)
        time_since_update = datetime.now() - last_update
        
        # Build performance metrics
        price_changes = portfolio_stats.get('price_changes', [])
        positive_changes = len([c for c in price_changes if c > 0])
        negative_changes = len([c for c in price_changes if c < 0])
        
        return {
            'dashboard': dashboard,
            'portfolio': portfolio_stats,
            'player_counts': {
                'total': total_players,
                'active': active_players,
                'errors': error_players
            },
            'performance': {
                'positive_changes': positive_changes,
                'negative_changes': negative_changes,
                'neutral_changes': len(price_changes) - positive_changes - negative_changes
            },
            'time_info': {
                'last_updated': dashboard.last_updated,
                'hours_since_update': time_since_update.total_seconds() / 3600
            }
        }
    
    def format_dashboard_embed(self, dashboard_name: str, owner_id: int, page: int = 0) -> Optional[discord.Embed]:
        """Create a formatted Discord embed for a user dashboard
        
        Returns:
            discord.Embed: The formatted embed with table and statistics
        """
        summary = self.get_dashboard_summary(dashboard_name, owner_id)
        if not summary:
            return None
        
        dashboard = summary['dashboard']
        
        # Get detailed player data for this dashboard
        player_data_result = self.state_service.get_dashboard_players_data(
            dashboard_name, owner_id, page=0, per_page=100  # Get all players for table
        )
        
        if not player_data_result['players']:
            # Empty dashboard
            embed = discord.Embed(
                description=(
                    f"```ansi\n"
                    f"Dashboard von {owner_id}\n"
                    f"─────────────────────────────────────────────────────\n"
                    f"Keine Spieler im Dashboard.\n"
                    f"Verwende /add_to_dashboard zum Hinzufügen.\n"
                    f"```"
                ),
                color=0x999999,
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"{self._get_platform_name()} • Persönliches Dashboard")
            return (embed, None)  # No summary for empty dashboard
        
        # Build player data in same format as general dashboard
        player_table_data = []
        total_value = 0
        total_invested = 0
        total_min_value = 0
        total_max_value = 0
        available_count = 0
        price_changes_24h = []
        categories = {}
        show_purchase_prices = dashboard.show_purchase_prices
        
        for data in player_data_result['players']:
            try:
                player = data['player']
                state = data['state']
                current_price = state.price if state else None
                price_24h_ago = state.price_24h_ago if state else None
                min_price = state.min_price if state and state.min_price else (current_price if current_price else 0)
                max_price = state.max_price if state and state.max_price else (current_price if current_price else 0)
                purchase_price = data.get('purchase_price')
                profit_loss = data.get('profit_loss')
                
                # Kategorien sammeln
                category = getattr(player, 'category', 'Allgemein')
                if category not in categories:
                    categories[category] = {'count': 0, 'value': 0, 'invested': 0}
                categories[category]['count'] += 1
                
                if current_price is None:
                    # Spieler ohne Preis
                    name = player.name[:15].ljust(15)
                    if show_purchase_prices and purchase_price is not None:
                        player_table_data.append({
                            'line': f"{name}    N/A        N/A    Gekauft: {purchase_price:,}",
                            'price': 0,
                            'volatility': 0,
                            'has_alert': bool(getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None))
                        })
                    else:
                        player_table_data.append({
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
                
                if purchase_price is not None:
                    total_invested += purchase_price
                    categories[category]['invested'] += purchase_price
                
                # Spielername (angepasst für Purchase Price Display)
                alert_indicator = "🔔" if getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None) else "  "
                if show_purchase_prices:
                    player_name_short = player.name[:11].ljust(11)  # Kürzer wegen zusätzlicher Spalte
                else:
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
                    
                    # Formatiere 24h-Änderung (kompakter wenn Purchase Prices angezeigt werden)
                    if show_purchase_prices:
                        # Kompaktere Formatierung
                        if pct_24h > 0:
                            h24_str = f"📈{pct_24h:+.1f}%".rjust(12)
                        elif pct_24h < 0:
                            h24_str = f"📉{pct_24h:+.1f}%".rjust(12)
                        else:
                            h24_str = f"➖{pct_24h:+.1f}%".rjust(12)
                    else:
                        # Originale Formatierung
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
                    if show_purchase_prices:
                        h24_str = "➖ +0.0%".rjust(12)
                    else:
                        h24_str = "➖ +0.0%".rjust(18)
                    price_changes_24h.append(0)
                
                # Purchase Price und Profit/Loss Spalte
                if show_purchase_prices and purchase_price is not None:
                    if profit_loss is not None:
                        if profit_loss > 0:
                            profit_str = f"💰{profit_loss:+,}".rjust(15)
                        elif profit_loss < 0:
                            profit_str = f"📉{profit_loss:+,}".rjust(15)
                        else:
                            profit_str = f"➖{profit_loss:+,}".rjust(15)
                    else:
                        profit_str = "N/A".rjust(15)
                    
                    # Erweiterte Zeile mit Purchase Price Info
                    line = f"{name_with_alert} {price_str} {h24_str} {profit_str}"
                elif show_purchase_prices:
                    # Zeige an, dass kein Kaufpreis gesetzt ist
                    line = f"{name_with_alert} {price_str} {h24_str} {'XXX'.rjust(15)}"
                else:
                    # Originale Zeile ohne Purchase Price
                    line = f"{name_with_alert} {price_str} {h24_str}"
                
                # Volatilität berechnen (Differenz zwischen Min/Max als Prozent)
                volatility = 0
                if min_price > 0 and max_price > min_price:
                    volatility = ((max_price - min_price) / min_price) * 100
                
                # Ensure profit_loss is properly handled for later calculations
                safe_profit_loss = profit_loss if profit_loss is not None else 0
                
                player_table_data.append({
                    'line': line,
                    'price': current_price,
                    'volatility': volatility,
                    'has_alert': bool(getattr(player, 'alert_above', None) or getattr(player, 'alert_below', None)),
                    'profit_loss': safe_profit_loss  # Use safe value for later calculations
                })
                    
            except Exception as e:
                self._logger.error(f"Error processing player {getattr(player, 'name', 'Unknown')}: {e}")
                # Fallback-Eintrag
                player_table_data.append({
                    'line': f"{getattr(player, 'name', 'Error')[:15].ljust(15)}    ERROR      ERROR  ERROR",
                    'price': 0,
                    'volatility': 0,
                    'has_alert': False
                })
                continue
        
        # Sortiere nach Preis (höchster zuerst)
        player_table_data.sort(key=lambda x: x['price'], reverse=True)
        
        # Erstelle erweiterte Tabelle mit angepasstem Header
        table_lines = []
        table_lines.append("```ansi")
        
        if show_purchase_prices:
            table_lines.append("Name           Preis        Trend       Profit/Loss")
            table_lines.append("─" * 56)
        else:
            table_lines.append("─" * 56)
            table_lines.append("Name             Preis              Trend      ")
            table_lines.append("─" * 56)
        
        # Füge Spielerzeilen zur Tabelle hinzu
        for data in player_table_data:
            table_lines.append(data['line'])
        
        # Erweiterte Statistiken mit korrekter Trennlinie
        if show_purchase_prices:
            table_lines.append("─" * 56)
        else:
            table_lines.append("─" * 56)
        
        if available_count > 0:
            # Portfolio-Statistiken
            avg_value = total_value / available_count
            portfolio_growth = sum(price_changes_24h) / len(price_changes_24h) if price_changes_24h else 0
            
            
            
            # 24h Performance
            growth_emoji = "📈" if portfolio_growth > 0 else "📉" if portfolio_growth < 0 else "➖"
            performance_str = f"{growth_emoji}{portfolio_growth:+.1f}%"
            
            table_lines.append(f"Spieler: {available_count}/{len(player_data_result['players'])}")
            
            # Profit/Loss Summary wenn Purchase Prices aktiviert sind
            if show_purchase_prices and total_invested > 0:
                # Ensure values are not None before calculation
                safe_total_value = total_value or 0
                safe_total_invested = total_invested or 0
                
                total_profit_loss = safe_total_value - safe_total_invested
                profit_loss_pct = (total_profit_loss / safe_total_invested) * 100 if safe_total_invested > 0 else 0
                
                # Berechne profitable vs unprofitable Spieler
                profitable_players = len([data for data in player_table_data if data.get('profit_loss', 0) > 0])
                losing_players = len([data for data in player_table_data if data.get('profit_loss', 0) < 0])
                
                # Formatiere Profit/Loss
                if abs(total_profit_loss) >= 1_000_000:
                    profit_str = f"{total_profit_loss/1_000_000:+.1f}M Coins"
                elif abs(total_profit_loss) >= 1_000:
                    profit_str = f"{total_profit_loss/1_000:+.0f}K Coins"
                else:
                    profit_str = f"{total_profit_loss:+,.0f} Coins"
                
                profit_emoji = "💰" if total_profit_loss > 0 else "📉" if total_profit_loss < 0 else "➖"
                table_lines.append(f"Investiert: {safe_total_invested/1_000:,.0f}K Coins")
                table_lines.append(f"P/L: {profit_str} ({profit_loss_pct:+.1f}%)")
                table_lines.append(f"Erfolg: {profitable_players}↑/{losing_players}↓")
            
        else:
            table_lines.append("Keine Preisdaten verfügbar")
        
        # Portfolio-Information am Ende hinzufügen mit korrekter Trennlinie
        if show_purchase_prices:
            table_lines.append("─" * 56)
        else:
            table_lines.append("─" * 56)
        
        table_lines.append("```")
        
        # Prepare summary text (outside code block, but in same message)
        summary_text = ""
        if available_count > 0:
            # Portfolio-Wert
            if total_value >= 1_000_000:
                total_str = f"Gesamtwert: {total_value/1_000_000:.1f}M Coins"
            elif total_value >= 1_000:
                total_str = f"Gesamtwert: {total_value/1_000:.0f}K Coins"
            else:
                total_str = f"Gesamtwert: {total_value:,.0f} Coins"
            
            # Durchschnitt
            if avg_value >= 1_000_000:
                avg_str = f"Durchschnitt: {avg_value/1_000_000:.1f}M Coins"
            elif avg_value >= 1_000:
                avg_str = f"Durchschnitt: {avg_value/1_000:.0f}K Coins"  
            else:
                avg_str = f"Durchschnitt: {avg_value:.0f} Coins"
            
            # Performance-Indikatoren
            positive_changes = len([c for c in price_changes_24h if c > 0])
            negative_changes = len([c for c in price_changes_24h if c < 0])
            
            # Build summary text (outside code block)
            summary_lines = []
            summary_lines.append(total_str)
            summary_lines.append(avg_str)
            summary_lines.append(f"24h: {performance_str} ({positive_changes}↑/{negative_changes}↓)")
            summary_text = "\n".join(summary_lines)
        
        # Description zusammenfügen: Tabelle + Statistiken
        full_description = "\n".join(table_lines)
        if summary_text:
            full_description += "\n" + summary_text
        
        # Create embed
        embed = discord.Embed(
            title=f"{dashboard.currency_symbol} {dashboard.name}",
            description=full_description,
            color=0x00ff00 if price_changes_24h and sum(price_changes_24h)/len(price_changes_24h) >= 0 else 0xff0000,
            timestamp=datetime.now()
        )
        
        # Zusätzliche Embed-Felder für erweiterte Infos
        if available_count > 0 and len(categories) > 1:
            # Kategorien-Overview
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
        
        # Dashboard-Info Field
        dashboard_info = []
        dashboard_info.append(f"👤 **Besitzer:** <@{owner_id}>")
        
        # Time since last update
        last_update = datetime.fromisoformat(dashboard.last_updated)
        time_since_update = datetime.now() - last_update
        hours_ago = time_since_update.total_seconds() / 3600
        
        if hours_ago < 1:
            time_str = "Vor wenigen Minuten"
        elif hours_ago < 24:
            time_str = f"Vor {hours_ago:.0f} Stunden"
        else:
            time_str = f"Vor {hours_ago/24:.1f} Tagen"
            
        dashboard_info.append(f"**Aktualisiert:** {time_str}")
        
        
        embed.set_footer(text=f"{self._get_platform_name()} • Persönliches Dashboard")
        
        return embed
    
    def get_user_dashboard_list(self, owner_id: int) -> List[Dict[str, Any]]:
        """Get a summary list of all user dashboards"""
        dashboards = self.state_service.get_user_dashboards(owner_id)
        dashboard_list = []
        
        for dashboard in dashboards:
            # Get basic stats
            portfolio_stats = self.state_service.get_dashboard_portfolio_stats(dashboard.name, owner_id)
            
            dashboard_list.append({
                'name': dashboard.name,
                'player_count': len(dashboard.player_urls),
                'total_value': portfolio_stats.get('total_value', 0),
                'avg_change': portfolio_stats.get('avg_change', 0),
                'created_at': dashboard.created_at,
                'last_updated': dashboard.last_updated,
                'is_active': dashboard.is_active
            })
        
        # Sort by last updated (most recent first)
        dashboard_list.sort(key=lambda x: x['last_updated'], reverse=True)
        return dashboard_list
    
    def add_players_to_dashboard(self, dashboard_name: str, owner_id: int, 
                                player_identifiers: List[str]) -> Tuple[List[str], List[str]]:
        """Add multiple players to a dashboard, return (added, not_found)"""
        added_players = []
        not_found_players = []
        
        for identifier in player_identifiers:
            # Try to find player
            player = (self.state_service.get_player_by_name(identifier) or 
                     self.state_service.get_player_by_url(identifier))
            
            if player:
                success = self.state_service.add_player_to_dashboard(
                    dashboard_name, owner_id, player.url
                )
                if success:
                    added_players.append(player.name)
            else:
                not_found_players.append(identifier)
        
        return added_players, not_found_players
    
    def remove_players_from_dashboard(self, dashboard_name: str, owner_id: int, 
                                    player_identifiers: List[str]) -> Tuple[List[str], List[str]]:
        """Remove multiple players from a dashboard, return (removed, not_found)"""
        removed_players = []
        not_found_players = []
        
        dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return [], player_identifiers
        
        for identifier in player_identifiers:
            # Try to find player in dashboard
            found = False
            for url in dashboard.player_urls:
                player = self.state_service.get_player_by_url(url)
                if player and (player.name.lower() == identifier.lower() or 
                              player.url == identifier):
                    success = self.state_service.remove_player_from_dashboard(
                        dashboard_name, owner_id, url
                    )
                    if success:
                        removed_players.append(player.name)
                    found = True
                    break
            
            if not found:
                not_found_players.append(identifier)
        
        return removed_players, not_found_players
    
    def validate_dashboard_ownership(self, dashboard_name: str, user_id: int) -> bool:
        """Check if a user owns a specific dashboard"""
        dashboard = self.state_service.get_user_dashboard(dashboard_name, user_id)
        return dashboard is not None and dashboard.owner_id == user_id
    
    def get_available_players_for_dashboard(self, dashboard_name: str, owner_id: int) -> List[PlayerConfig]:
        """Get list of players that can be added to a dashboard (not already included)"""
        dashboard = self.state_service.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return []
        
        all_players = self.state_service.get_all_active_players()
        dashboard_urls = set(dashboard.player_urls)
        
        return [player for player in all_players if player.url not in dashboard_urls]
    
    def cleanup_inactive_dashboards(self, days_threshold: int = 30) -> int:
        """Remove dashboards that haven't been updated in X days"""
        cutoff_date = datetime.now() - timedelta(days=days_threshold)
        removed_count = 0
        
        # Get all dashboards
        all_dashboards = list(self.state_service.user_dashboards.items())
        
        for dashboard_key, dashboard in all_dashboards:
            try:
                last_updated = datetime.fromisoformat(dashboard.last_updated)
                if last_updated < cutoff_date and not dashboard.is_active:
                    # Remove dashboard
                    success = self.state_service.delete_user_dashboard(
                        dashboard.name, dashboard.owner_id
                    )
                    if success:
                        removed_count += 1
                        self._logger.info(f"Removed inactive dashboard: {dashboard.name}")
            except ValueError:
                # Invalid date format, skip
                continue
        
        return removed_count