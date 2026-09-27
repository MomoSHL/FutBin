"""
State management service with TTL caching and efficient player lookups.
Handles persistent state, price change detection, and alert triggering.
"""

import json
import logging
import time
import re
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Set
from dataclasses import field

from .price_service import PlayerPrice


def normalize_player_url(url: str) -> str:
    """
    Normalize FUTBin player URL to compare by ID only.
    Removes name slugs and /market suffix for consistent comparison.
    
    Examples:
        https://www.futbin.com/26/player/91/achraf-hakimi -> https://www.futbin.com/26/player/91
        https://www.futbin.com/26/player/68/vitinha/market -> https://www.futbin.com/26/player/68
    """
    # Extract player ID using regex
    match = re.search(r'(https?://(?:www\.)?futbin\.com/\d+/player/(\d+))', url)
    if match:
        return match.group(1)  # Return base URL with just ID
    return url  # Return original if pattern doesn't match


@dataclass
class PlayerConfig:
    """Enhanced player configuration with efficient lookups"""
    name: str
    url: str
    active: bool = True
    threshold_percent: Optional[float] = None
    alert_above: Optional[int] = None
    alert_below: Optional[int] = None
    alert_user_id: Optional[int] = None
    category: str = "Allgemein"
    notes: str = ""
    card_version: str = ""  # Event name (e.g., "TOTW", "Icon", "TOTY", etc.)


@dataclass
class PlayerState:
    """Player state with price history and metadata"""
    name: str
    url: str
    price: Optional[int] = None
    last_price: Optional[int] = None
    price_24h_ago: Optional[int] = None
    price_24h_timestamp: int = 0
    image: Optional[str] = None
    timestamp: int = field(default_factory=lambda: int(time.time()))
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    alert_triggered: bool = False
    fetch_errors: int = 0
    last_successful_fetch: int = field(default_factory=lambda: int(time.time()))
    card_version: str = ""  # Event name (e.g., "TOTW", "Icon", "TOTY", etc.)


@dataclass
class UserDashboard:
    """User-specific dashboard configuration with portfolio tracking"""
    name: str
    owner_id: int
    player_urls: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    notes: str = ""
    is_active: bool = True
    
    # Discord message tracking
    dashboard_message_id: Optional[int] = None
    dashboard_channel_id: Optional[int] = None
    
    # Portfolio settings
    track_portfolio_value: bool = True
    currency_symbol: str = "💰"
    show_percentage_changes: bool = True
    show_24h_changes: bool = True
    
    # Purchase prices tracking - URL -> purchase_price
    purchase_prices: Dict[str, int] = field(default_factory=dict)  # player_url -> bought_for_price
    purchase_dates: Dict[str, str] = field(default_factory=dict)   # player_url -> purchase_date
    
    # Display settings
    max_players_per_page: int = 10
    sort_by: str = "price_desc"  # price_desc, price_asc, name, change_desc, change_asc, profit_desc, profit_asc
    show_images: bool = True
    show_purchase_prices: bool = True


@dataclass
class CacheEntry:
    """TTL cache entry for computed data"""
    data: Any
    timestamp: float
    ttl: float
    
    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


class StateService:
    """Efficient state management with TTL caching and dict-based lookups"""
    
    def __init__(self, state_file_path: Path, config_file_path: Path, global_threshold: float = 5.0):
        self.state_file = state_file_path
        self.config_file = config_file_path
        self.global_threshold = global_threshold
        self.platform = "pc"
        self._logger = logging.getLogger(__name__)
        
        # Efficient lookups: URL -> PlayerConfig/PlayerState
        self.players_by_url: Dict[str, PlayerConfig] = {}
        self.players_by_name: Dict[str, PlayerConfig] = {}
        self.state_by_url: Dict[str, PlayerState] = {}
        
        # User Dashboard management
        self.user_dashboards: Dict[str, UserDashboard] = {}  # dashboard_name -> UserDashboard
        self.dashboards_by_user: Dict[int, List[str]] = defaultdict(list)  # user_id -> [dashboard_names]
        self.dashboard_file = self.state_file.parent / "user_dashboards.json"
        
        # TTL caches for computed data
        self._caches: Dict[str, CacheEntry] = {}
        
        # Change tracking
        self._dirty_configs: Set[str] = set()
        self._dirty_states: Set[str] = set()
        self._dirty_dashboards: Set[str] = set()
        
        # Load initial data
        self._load_config()
        self._load_state()
        self._load_dashboards()

    def get_player_by_url(self, url: str) -> Optional[PlayerConfig]:
        """Get player config by URL (O(1) lookup with normalized URL fallback)"""
        # Try exact match first
        player = self.players_by_url.get(url)
        if player:
            return player
        
        # Fallback: search by normalized URL
        normalized_url = normalize_player_url(url)
        for existing_url, existing_player in self.players_by_url.items():
            if normalize_player_url(existing_url) == normalized_url:
                self._logger.debug(f"Found player by normalized URL: {url} -> {existing_url}")
                return existing_player
        
        return None

    def get_player_by_name(self, name: str) -> Optional[PlayerConfig]:
        """Get player config by name (O(1) lookup)"""
        return self.players_by_name.get(name.lower())

    def get_state_by_url(self, url: str) -> Optional[PlayerState]:
        """Get player state by URL (O(1) lookup with normalized URL fallback)"""
        # Try exact match first
        state = self.state_by_url.get(url)
        if state:
            return state
        
        # Fallback: search by normalized URL
        normalized_url = normalize_player_url(url)
        for existing_url, existing_state in self.state_by_url.items():
            if normalize_player_url(existing_url) == normalized_url:
                self._logger.debug(f"Found state by normalized URL: {url} -> {existing_url}")
                return existing_state
        
        return None

    def get_all_active_players(self) -> List[PlayerConfig]:
        """Get all active players"""
        return [p for p in self.players_by_url.values() if p.active]

    def get_players_by_category(self, category: str) -> List[PlayerConfig]:
        """Get players filtered by category with caching"""
        cache_key = f"category:{category.lower()}"
        cached = self._get_cached(cache_key, ttl=300)  # 5 min cache
        
        if cached is not None:
            return cached
        
        result = [p for p in self.players_by_url.values() if p.category.lower() == category.lower()]
        self._set_cache(cache_key, result, 300)
        return result

    def add_player(self, player: PlayerConfig) -> bool:
        """Add a new player with duplicate checking using normalized URLs"""
        # Check if player already exists using normalized URL
        normalized_url = normalize_player_url(player.url)
        
        # Check if any existing player has the same normalized URL
        for existing_url, existing_player in self.players_by_url.items():
            if normalize_player_url(existing_url) == normalized_url:
                self._logger.warning(
                    f"Player with normalized URL {normalized_url} already exists as {existing_url}. "
                    f"Skipping duplicate: {player.name} ({player.url})"
                )
                return False
        
        # Add to lookups
        self.players_by_url[player.url] = player
        self.players_by_name[player.name.lower()] = player
        self._dirty_configs.add(player.url)
        
        # Initialize state
        self.state_by_url[player.url] = PlayerState(
            name=player.name,
            url=player.url
        )
        self._dirty_states.add(player.url)
        
        self._invalidate_category_caches()
        self._logger.info(f"Added player: {player.name} ({player.url})")
        return True

    def remove_player(self, url: str) -> bool:
        """Remove a player and cleanup state"""
        player = self.players_by_url.get(url)
        if not player:
            return False
            
        # Remove from lookups
        del self.players_by_url[url]
        self.players_by_name.pop(player.name.lower(), None)
        
        # Remove state
        self.state_by_url.pop(url, None)
        
        self._dirty_configs.add(url)
        self._dirty_states.add(url)
        self._invalidate_category_caches()
        
        self._logger.info(f"Removed player: {player.name}")
        return True

    def find_duplicate_players(self) -> Dict[str, list]:
        """Find duplicate players by normalized URL
        
        Returns:
            Dict mapping normalized URLs to lists of player URLs that match
        """
        normalized_groups = {}
        
        for url in self.players_by_url.keys():
            normalized = normalize_player_url(url)
            if normalized not in normalized_groups:
                normalized_groups[normalized] = []
            normalized_groups[normalized].append(url)
        
        # Filter to only return groups with duplicates
        duplicates = {norm_url: urls for norm_url, urls in normalized_groups.items() if len(urls) > 1}
        
        if duplicates:
            self._logger.info(f"Found {len(duplicates)} groups of duplicate players")
            for norm_url, urls in duplicates.items():
                player_names = [self.players_by_url[url].name for url in urls]
                self._logger.info(f"  {norm_url}: {player_names} -> {urls}")
        
        return duplicates

    def merge_duplicate_players(self, duplicate_groups: Dict[str, list]) -> Dict[str, Any]:
        """Merge duplicate players, keeping the one with the most complete URL
        
        Args:
            duplicate_groups: Dict from find_duplicate_players()
            
        Returns:
            Dict with merge statistics and details
        """
        merge_stats = {
            'groups_processed': 0,
            'players_removed': 0,
            'players_kept': 0,
            'details': []
        }
        
        for norm_url, duplicate_urls in duplicate_groups.items():
            if len(duplicate_urls) <= 1:
                continue
                
            # Sort to prefer URLs with slugs (longer URLs are more complete)
            # Also prefer URLs without /market suffix
            sorted_urls = sorted(
                duplicate_urls,
                key=lambda url: (
                    '/market' not in url,  # Prefer without /market
                    len(url)  # Prefer longer (more complete) URLs
                ),
                reverse=True
            )
            
            url_to_keep = sorted_urls[0]
            urls_to_remove = sorted_urls[1:]
            
            player_to_keep = self.players_by_url[url_to_keep]
            
            merge_info = {
                'kept': {
                    'url': url_to_keep,
                    'name': player_to_keep.name
                },
                'removed': []
            }
            
            # Remove duplicates
            for url_to_remove in urls_to_remove:
                player = self.players_by_url.get(url_to_remove)
                if player:
                    merge_info['removed'].append({
                        'url': url_to_remove,
                        'name': player.name
                    })
                    self.remove_player(url_to_remove)
                    merge_stats['players_removed'] += 1
            
            merge_stats['groups_processed'] += 1
            merge_stats['players_kept'] += 1
            merge_stats['details'].append(merge_info)
            
            self._logger.info(
                f"Merged duplicates for {player_to_keep.name}: "
                f"kept {url_to_keep}, removed {len(urls_to_remove)} duplicates"
            )
        
        return merge_stats

    def update_player_price(self, url: str, price_data: PlayerPrice) -> Dict[str, Any]:
        """Update player price and return change info"""
        player = self.players_by_url.get(url)
        if not player:
            self._logger.warning(f"No player found for URL: {url}")
            return {}
            
        state = self.state_by_url.get(url)
        if not state:
            # Create new state
            state = PlayerState(name=player.name, url=url)
            self.state_by_url[url] = state
        
        # Store old values for change detection
        old_price = state.price
        old_image = state.image
        
        if price_data.success and price_data.price is not None:
            # Update successful fetch
            state.last_successful_fetch = int(time.time())
            state.fetch_errors = 0
            
            # Update price data
            state.last_price = old_price
            state.price = price_data.price
            state.image = price_data.image_url or old_image
            
            # Update card version if available
            if price_data.card_version:
                state.card_version = price_data.card_version
                # Also update in player config if available
                if player and hasattr(player, 'card_version'):
                    player.card_version = price_data.card_version
            
            # Update min/max
            if state.min_price is None or price_data.price < state.min_price:
                state.min_price = price_data.price
            if state.max_price is None or price_data.price > state.max_price:
                state.max_price = price_data.price
                
            # Handle 24h price tracking
            current_time = int(time.time())
            if current_time - state.price_24h_timestamp >= 86400:  # 24 hours
                state.price_24h_ago = old_price or price_data.price
                state.price_24h_timestamp = current_time
            elif state.price_24h_ago is None:
                state.price_24h_ago = old_price or price_data.price
                state.price_24h_timestamp = current_time
        else:
            # Handle failed fetch
            state.fetch_errors += 1
            self._logger.warning(f"Failed to fetch price for {player.name}: {price_data.error}")
        
        # Update metadata
        state.timestamp = int(time.time())
        state.last_updated = datetime.now().isoformat()
        
        # Mark as dirty
        self._dirty_states.add(url)
        
        # Check for significant changes
        change_info = self._analyze_price_change(player, old_price, state.price)
        
        # Check alerts
        alerts = self._check_price_alerts(player, old_price, state.price)
        if alerts:
            change_info['alerts'] = alerts
            state.alert_triggered = True
        
        return change_info

    def price_changed_significantly(self, url: str) -> bool:
        """Check if price changed significantly since last check"""
        player = self.players_by_url.get(url)
        state = self.state_by_url.get(url)
        
        if not player or not state or state.price is None:
            return False
            
        last_price = state.last_price
        if last_price is None or last_price == 0:
            return True  # First price is always significant
            
        change_pct = abs(state.price - last_price) / last_price * 100
        threshold = player.threshold_percent or self.global_threshold
        return change_pct >= threshold

    def get_portfolio_stats(self, category_filter: Optional[str] = None) -> Dict[str, Any]:
        """Get portfolio statistics with caching"""
        cache_key = f"portfolio_stats:{category_filter or 'all'}"
        cached = self._get_cached(cache_key, ttl=60)  # 1 min cache
        
        if cached is not None:
            return cached
        
        players = (self.get_players_by_category(category_filter) 
                  if category_filter else self.get_all_active_players())
        
        stats = self._calculate_portfolio_stats(players)
        self._set_cache(cache_key, stats, 60)
        return stats

    def _calculate_portfolio_stats(self, players: List[PlayerConfig]) -> Dict[str, Any]:
        """Calculate portfolio statistics for given players"""
        total_value = 0
        total_min = 0
        total_max = 0
        available_count = 0
        price_changes = []
        alert_count = 0
        
        for player in players:
            state = self.state_by_url.get(player.url)
            if not state or state.price is None:
                continue
                
            available_count += 1
            total_value += state.price
            total_min += state.min_price or state.price
            total_max += state.max_price or state.price
            
            # Track price changes
            if state.last_price is not None and state.last_price > 0:
                change_pct = ((state.price - state.last_price) / state.last_price) * 100
                price_changes.append(change_pct)
            
            # Count alerts
            if (player.alert_above or player.alert_below) and player.alert_user_id:
                alert_count += 1
        
        # Calculate aggregates
        avg_value = total_value / available_count if available_count > 0 else 0
        avg_change = sum(price_changes) / len(price_changes) if price_changes else 0
        
        return {
            'total_value': total_value,
            'total_min': total_min,
            'total_max': total_max,
            'available_count': available_count,
            'total_count': len(players),
            'avg_value': avg_value,
            'avg_change': avg_change,
            'alert_count': alert_count,
            'price_changes': price_changes
        }

    def _analyze_price_change(self, player: PlayerConfig, old_price: Optional[int], new_price: Optional[int]) -> Dict[str, Any]:
        """Analyze price change and return change information"""
        if new_price is None:
            return {}
            
        change_info = {
            'player': player,
            'old_price': old_price,
            'new_price': new_price,
            'significant': False,
            'change_percent': 0.0,
            'change_absolute': 0
        }
        
        if old_price is not None and old_price > 0:
            change_absolute = new_price - old_price
            change_percent = (change_absolute / old_price) * 100
            
            change_info.update({
                'change_percent': change_percent,
                'change_absolute': change_absolute,
                'significant': self.price_changed_significantly(player.url)
            })
        
        return change_info

    def _check_price_alerts(self, player: PlayerConfig, old_price: Optional[int], new_price: Optional[int]) -> List[Dict[str, Any]]:
        """Check if price alerts should be triggered"""
        if not player.alert_user_id or new_price is None:
            return []
        
        alerts = []
        
        # Alert Above - price rises above threshold
        if (player.alert_above and new_price >= player.alert_above and 
            (old_price is None or old_price < player.alert_above)):
            alerts.append({
                'type': 'above',
                'threshold': player.alert_above,
                'current_price': new_price,
                'user_id': player.alert_user_id
            })
        
        # Alert Below - price falls below threshold
        if (player.alert_below and new_price <= player.alert_below and 
            (old_price is None or old_price > player.alert_below)):
            alerts.append({
                'type': 'below',
                'threshold': player.alert_below,
                'current_price': new_price,
                'user_id': player.alert_user_id
            })
        
        return alerts

    def save_dirty_configs(self) -> None:
        """Save only dirty configurations"""
        if not self._dirty_configs:
            return
            
        try:
            # Load current config
            config_data = {}
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    import yaml
                    config_data = yaml.safe_load(f) or {}
            
            # Update players section
            if not isinstance(config_data.get('players'), list):
                config_data['players'] = []
            
            # Create URL -> config mapping for easy updates
            existing_players = {p.get('url'): p for p in config_data['players'] if isinstance(p, dict)}
            
            # Rebuild players list
            new_players = []
            
            # First, keep all players that are not dirty (unchanged)
            for p in config_data['players']:
                url = p.get('url')
                if url not in self._dirty_configs:
                    new_players.append(p)
            
            # Then, add/update dirty players (only if they still exist)
            for url in list(self._dirty_configs):
                player = self.players_by_url.get(url)
                if player:
                    player_dict = {
                        'name': player.name,
                        'url': player.url,
                        'active': player.active,
                        'threshold_percent': player.threshold_percent,
                        'alert_above': player.alert_above,
                        'alert_below': player.alert_below,
                        'alert_user_id': player.alert_user_id,
                        'category': player.category,
                        'notes': player.notes
                    }
                    new_players.append(player_dict)
                # If player is None, it was deleted and won't be added
            
            config_data['players'] = new_players
            
            # Save updated config
            with open(self.config_file, 'w', encoding='utf-8') as f:
                import yaml
                yaml.safe_dump(config_data, f, default_flow_style=False, allow_unicode=True)
            
            self._dirty_configs.clear()
            self._logger.debug("Saved dirty configurations")
            
        except Exception as e:
            self._logger.error(f"Failed to save config: {e}", exc_info=True)

    def save_dirty_states(self) -> None:
        """Save only dirty states"""
        if not self._dirty_states:
            return
            
        try:
            # Convert states to serializable format
            state_data = {}
            for url, state in self.state_by_url.items():
                if url in self._dirty_states:
                    state_data[url] = asdict(state)
            
            # Load existing states and update
            if self.state_file.exists():
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = {}
            
            existing_data.update(state_data)
            
            # Remove deleted players
            urls_to_remove = []
            for url in existing_data:
                if url not in self.state_by_url:
                    urls_to_remove.append(url)
            
            for url in urls_to_remove:
                del existing_data[url]
            
            # Save updated states
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2, ensure_ascii=False)
            
            self._dirty_states.clear()
            self._logger.debug("Saved dirty states")
            
        except Exception as e:
            self._logger.error(f"Failed to save state: {e}")

    def save_dirty_dashboards(self) -> None:
        """Save only dirty user dashboards"""
        if not self._dirty_dashboards:
            return
            
        try:
            # Convert dashboards to serializable format
            dashboard_data = {}
            for dashboard_key, dashboard in self.user_dashboards.items():
                dashboard_data[dashboard_key] = asdict(dashboard)
            
            # Save all dashboards (since we manage them as a unit)
            with open(self.dashboard_file, 'w', encoding='utf-8') as f:
                json.dump(dashboard_data, f, indent=2, ensure_ascii=False)
            
            self._dirty_dashboards.clear()
            self._logger.debug("Saved user dashboards")
            
        except Exception as e:
            self._logger.error(f"Failed to save dashboards: {e}")

    def save_all(self) -> None:
        """Save all dirty data"""
        self.save_dirty_configs()
        self.save_dirty_states()
        self.save_dirty_dashboards()

    def load_all(self) -> None:
        """Reload all data from files"""
        # Clear existing data
        self.players_by_url.clear()
        self.players_by_name.clear()
        self.state_by_url.clear()
        self.user_dashboards.clear()
        self.dashboards_by_user.clear()
        self._caches.clear()
        
        # Reload from files
        self._load_config()
        self._load_state()
        self._load_dashboards()
        
        self._logger.info("Reloaded all data from files")

    def _load_config(self) -> None:
        """Load player configurations from file"""
        try:
            if not self.config_file.exists():
                self._logger.info("No config file found, starting with empty configuration")
                return
                
            with open(self.config_file, 'r', encoding='utf-8') as f:
                import yaml
                config_data = yaml.safe_load(f) or {}
            
            settings = config_data.get('settings', {})
            if 'platform' in settings and settings['platform']:
                self.platform = str(settings['platform']).lower()
            if 'global_threshold_percent' in settings and settings['global_threshold_percent'] is not None:
                self.global_threshold = float(settings['global_threshold_percent'])
            
            players_data = config_data.get('players', [])
            
            for p in players_data:
                if not p.get('active', True):
                    continue
                    
                player = PlayerConfig(
                    name=p.get('name', ''),
                    url=p.get('url', ''),
                    active=p.get('active', True),
                    threshold_percent=p.get('threshold_percent'),
                    alert_above=p.get('alert_above'),
                    alert_below=p.get('alert_below'),
                    alert_user_id=p.get('alert_user_id'),
                    category=p.get('category', 'Allgemein'),
                    notes=p.get('notes', '')
                )
                
                if player.url and player.name:
                    self.players_by_url[player.url] = player
                    self.players_by_name[player.name.lower()] = player
            
            self._logger.info(f"Loaded {len(self.players_by_url)} players from config")
            
        except Exception as e:
            self._logger.error(f"Failed to load config: {e}")

    def _load_state(self) -> None:
        """Load player states from file"""
        try:
            if not self.state_file.exists():
                self._logger.info("No state file found, starting with empty state")
                return
                
            with open(self.state_file, 'r', encoding='utf-8') as f:
                state_data = json.load(f)
            
            for url, state_dict in state_data.items():
                if url in self.players_by_url:  # Only load states for active players
                    self.state_by_url[url] = PlayerState(**state_dict)
            
            self._logger.info(f"Loaded {len(self.state_by_url)} player states")
            
        except Exception as e:
            self._logger.error(f"Failed to load state: {e}")

    def _load_dashboards(self) -> None:
        """Load user dashboards from file"""
        try:
            if not self.dashboard_file.exists():
                self._logger.info("No dashboard file found, starting with empty dashboards")
                return
                
            with open(self.dashboard_file, 'r', encoding='utf-8') as f:
                dashboard_data = json.load(f)
            
            for dashboard_key, dashboard_dict in dashboard_data.items():
                dashboard = UserDashboard(**dashboard_dict)
                self.user_dashboards[dashboard_key] = dashboard
                
                # Update user dashboard mapping
                owner_id = dashboard.owner_id
                if dashboard_key not in self.dashboards_by_user[owner_id]:
                    self.dashboards_by_user[owner_id].append(dashboard_key)
            
            self._logger.info(f"Loaded {len(self.user_dashboards)} user dashboards")
            
        except Exception as e:
            self._logger.error(f"Failed to load dashboards: {e}")

    def _get_cached(self, key: str, ttl: float) -> Any:
        """Get cached value if not expired"""
        entry = self._caches.get(key)
        if entry and not entry.is_expired():
            return entry.data
        return None

    def _set_cache(self, key: str, data: Any, ttl: float) -> None:
        """Set cache entry with TTL"""
        self._caches[key] = CacheEntry(data, time.time(), ttl)

    def _invalidate_category_caches(self) -> None:
        """Invalidate all category-related caches"""
        keys_to_remove = [k for k in self._caches.keys() if k.startswith(('category:', 'portfolio_stats:'))]
        for key in keys_to_remove:
            del self._caches[key]

    def create_alert(self, url: str, alert_type: str, value: int, user_id: int) -> bool:
        """Create a price alert for a player"""
        player = self.players_by_url.get(url)
        if not player:
            self._logger.warning(f"No player found for URL: {url}")
            return False

        if alert_type.lower() == "above":
            player.alert_above = value
        elif alert_type.lower() == "below":
            player.alert_below = value
        else:
            self._logger.warning(f"Invalid alert type: {alert_type}")
            return False

        player.alert_user_id = user_id
        self._dirty_configs.add(url)
        self._logger.info(f"Created {alert_type} alert for {player.name}: {value} coins")
        return True

    def delete_alert(self, url: str, alert_type: str = None) -> bool:
        """Delete price alert(s) for a player"""
        player = self.players_by_url.get(url)
        if not player:
            self._logger.warning(f"No player found for URL: {url}")
            return False

        if alert_type is None or alert_type.lower() == "all":
            # Delete all alerts
            player.alert_above = None
            player.alert_below = None
            player.alert_user_id = None
        elif alert_type.lower() == "above":
            player.alert_above = None
        elif alert_type.lower() == "below":
            player.alert_below = None
        else:
            self._logger.warning(f"Invalid alert type: {alert_type}")
            return False

        self._dirty_configs.add(url)
        self._logger.info(f"Deleted {alert_type or 'all'} alert(s) for {player.name}")
        return True

    def get_player_alerts(self, url: str) -> Dict[str, Any]:
        """Get all alerts for a player"""
        player = self.players_by_url.get(url)
        if not player:
            return {}

        alerts = {}
        if player.alert_above:
            alerts['above'] = player.alert_above
        if player.alert_below:
            alerts['below'] = player.alert_below
        if player.alert_user_id:
            alerts['user_id'] = player.alert_user_id

        return alerts

    # ============ USER DASHBOARD MANAGEMENT ============
    
    def create_user_dashboard(self, name: str, owner_id: int, player_urls: List[str] = None, **kwargs) -> bool:
        """Create a new user dashboard"""
        # Validate dashboard name
        if not name or not name.strip():
            self._logger.warning("Dashboard name cannot be empty")
            return False
            
        dashboard_key = f"{owner_id}_{name.lower().replace(' ', '_')}"
        
        if dashboard_key in self.user_dashboards:
            self._logger.warning(f"Dashboard '{name}' already exists for user {owner_id}")
            return False
        
        # Validate player URLs (using normalized URL comparison)
        valid_urls = []
        if player_urls:
            for url in player_urls:
                # Check if player exists by exact match first
                if url in self.players_by_url:
                    valid_urls.append(url)
                else:
                    # Try to find player by normalized URL
                    normalized_url = normalize_player_url(url)
                    found = False
                    for existing_url in self.players_by_url.keys():
                        if normalize_player_url(existing_url) == normalized_url:
                            # Use the existing URL from the system
                            valid_urls.append(existing_url)
                            self._logger.info(f"Matched {url} to existing player {existing_url}")
                            found = True
                            break
                    
                    if not found:
                        self._logger.warning(f"Invalid player URL: {url}")
        
        # Create dashboard
        dashboard = UserDashboard(
            name=name,
            owner_id=owner_id,
            player_urls=valid_urls,
            **kwargs
        )
        
        self.user_dashboards[dashboard_key] = dashboard
        self.dashboards_by_user[owner_id].append(dashboard_key)
        self._dirty_dashboards.add(dashboard_key)
        
        self._logger.info(f"Created dashboard '{name}' for user {owner_id} with {len(valid_urls)} players")
        return True
    
    def get_user_dashboard(self, name: str, owner_id: int) -> Optional[UserDashboard]:
        """Get a specific user dashboard"""
        dashboard_key = f"{owner_id}_{name.lower().replace(' ', '_')}"
        return self.user_dashboards.get(dashboard_key)
    
    def get_user_dashboards(self, owner_id: int) -> List[UserDashboard]:
        """Get all dashboards for a specific user"""
        dashboard_keys = self.dashboards_by_user.get(owner_id, [])
        return [self.user_dashboards[key] for key in dashboard_keys if key in self.user_dashboards]
    
    def delete_user_dashboard(self, name: str, owner_id: int) -> bool:
        """Delete a user dashboard"""
        dashboard_key = f"{owner_id}_{name.lower().replace(' ', '_')}"
        
        if dashboard_key not in self.user_dashboards:
            return False
        
        # Remove dashboard
        del self.user_dashboards[dashboard_key]
        
        # Update user's dashboard list
        if owner_id in self.dashboards_by_user:
            self.dashboards_by_user[owner_id] = [
                key for key in self.dashboards_by_user[owner_id] if key != dashboard_key
            ]
        
        self._dirty_dashboards.add(dashboard_key)
        self._logger.info(f"Deleted dashboard '{name}' for user {owner_id}")
        return True
    
    def add_player_to_dashboard(self, dashboard_name: str, owner_id: int, player_url: str) -> bool:
        """Add a player to a user dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return False
        
        # Check if player exists by exact match first
        actual_url = player_url
        if player_url not in self.players_by_url:
            # Try to find player by normalized URL
            normalized_url = normalize_player_url(player_url)
            found = False
            for existing_url in self.players_by_url.keys():
                if normalize_player_url(existing_url) == normalized_url:
                    # Use the existing URL from the system
                    actual_url = existing_url
                    self._logger.info(f"Matched {player_url} to existing player {existing_url}")
                    found = True
                    break
            
            if not found:
                self._logger.warning(f"Invalid player URL: {player_url}")
                return False
        
        if actual_url not in dashboard.player_urls:
            dashboard.player_urls.append(actual_url)
            dashboard.last_updated = datetime.now().isoformat()
            self._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
            return True
        
        return False  # Player already in dashboard
    
    def remove_player_from_dashboard(self, dashboard_name: str, owner_id: int, player_url: str) -> bool:
        """Remove a player from a user dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return False
        
        if player_url in dashboard.player_urls:
            dashboard.player_urls.remove(player_url)
            dashboard.last_updated = datetime.now().isoformat()
            self._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
            return True
        
        return False  # Player not in dashboard
    
    def get_dashboard_portfolio_stats(self, dashboard_name: str, owner_id: int) -> Dict[str, Any]:
        """Get portfolio statistics for a specific user dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard or not dashboard.track_portfolio_value:
            return {}
        
        # Get players in this dashboard
        dashboard_players = []
        for url in dashboard.player_urls:
            player = self.players_by_url.get(url)
            if player and player.active:
                dashboard_players.append(player)
        
        if not dashboard_players:
            return {'total_value': 0, 'total_count': 0, 'available_count': 0}
        
        # Calculate portfolio stats
        stats = self._calculate_portfolio_stats(dashboard_players)
        
        # Add dashboard-specific metadata
        stats['dashboard_name'] = dashboard.name
        stats['owner_id'] = owner_id
        stats['currency_symbol'] = dashboard.currency_symbol
        stats['last_updated'] = dashboard.last_updated
        
        return stats
    
    def get_dashboard_players_data(self, dashboard_name: str, owner_id: int, 
                                    page: int = 0, per_page: Optional[int] = None) -> Dict[str, Any]:
        """Get detailed player data for a dashboard with pagination and sorting"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return {}
        
        # Use dashboard settings
        per_page = per_page or dashboard.max_players_per_page
        
        # Gather player data
        players_data = []
        for url in dashboard.player_urls:
            player = self.players_by_url.get(url)
            state = self.state_by_url.get(url)
            
            if not player or not state:
                continue
                
            # Calculate change data
            price_change = 0
            price_change_pct = 0.0
            change_24h = 0
            change_24h_pct = 0.0
            
            if state.price and state.last_price:
                price_change = state.price - state.last_price
                price_change_pct = (price_change / state.last_price) * 100 if state.last_price > 0 else 0
            
            if state.price and state.price_24h_ago:
                change_24h = state.price - state.price_24h_ago
                change_24h_pct = (change_24h / state.price_24h_ago) * 100 if state.price_24h_ago > 0 else 0
            
            # Calculate profit/loss if purchase price exists
            purchase_price = dashboard.purchase_prices.get(url)
            profit_loss = None
            profit_loss_pct = None
            
            if purchase_price is not None and state.price:
                profit_loss = state.price - purchase_price
                profit_loss_pct = (profit_loss / purchase_price) * 100 if purchase_price > 0 else 0
            
            # Determine sort key (including profit-based sorting)
            if dashboard.sort_by.startswith('profit') and profit_loss is not None:
                sort_key = profit_loss
            else:
                sort_key = self._get_sort_key(player, state, dashboard.sort_by)
            
            players_data.append({
                'player': player,
                'state': state,
                'price_change': price_change,
                'price_change_pct': price_change_pct,
                'change_24h': change_24h,
                'change_24h_pct': change_24h_pct,
                'purchase_price': purchase_price,
                'profit_loss': profit_loss,
                'profit_loss_pct': profit_loss_pct,
                'purchase_date': dashboard.purchase_dates.get(url),
                'sort_key': sort_key
            })
        
        # Sort players
        reverse = dashboard.sort_by.endswith('_desc')
        players_data.sort(key=lambda x: x['sort_key'], reverse=reverse)
        
        # Apply pagination
        start_idx = page * per_page
        end_idx = start_idx + per_page
        paginated_data = players_data[start_idx:end_idx]
        
        return {
            'players': paginated_data,
            'total_count': len(players_data),
            'page': page,
            'per_page': per_page,
            'total_pages': (len(players_data) + per_page - 1) // per_page,
            'dashboard': dashboard
        }
    
    def _get_sort_key(self, player: PlayerConfig, state: PlayerState, sort_by: str) -> Any:
        """Get sort key for player based on sort criteria"""
        if sort_by.startswith('name'):
            return player.name.lower()
        elif sort_by.startswith('price'):
            return state.price or 0
        elif sort_by.startswith('change'):
            if state.price and state.last_price:
                return ((state.price - state.last_price) / state.last_price) * 100
            return 0
        elif sort_by.startswith('profit'):
            # Sort by profit/loss (current_price - purchase_price)
            return 0  # Will be calculated in get_dashboard_players_data
        else:
            return player.name.lower()  # Default fallback
    
    # ============ PURCHASE PRICE MANAGEMENT ============
    
    def set_purchase_price(self, dashboard_name: str, owner_id: int, player_url: str, purchase_price: int) -> bool:
        """Set purchase price for a player in a dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return False
        
        if player_url not in dashboard.player_urls:
            self._logger.warning(f"Player {player_url} not in dashboard {dashboard_name}")
            return False
        
        dashboard.purchase_prices[player_url] = purchase_price
        dashboard.purchase_dates[player_url] = datetime.now().isoformat()
        dashboard.last_updated = datetime.now().isoformat()
        
        self._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
        self._logger.info(f"Set purchase price for player in dashboard {dashboard_name}: {purchase_price} coins")
        return True
    
    def get_purchase_price(self, dashboard_name: str, owner_id: int, player_url: str) -> Optional[int]:
        """Get purchase price for a player in a dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return None
        
        return dashboard.purchase_prices.get(player_url)
    
    def remove_purchase_price(self, dashboard_name: str, owner_id: int, player_url: str) -> bool:
        """Remove purchase price for a player in a dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return False
        
        removed = False
        if player_url in dashboard.purchase_prices:
            del dashboard.purchase_prices[player_url]
            removed = True
        
        if player_url in dashboard.purchase_dates:
            del dashboard.purchase_dates[player_url]
        
        if removed:
            dashboard.last_updated = datetime.now().isoformat()
            self._dirty_dashboards.add(f"{owner_id}_{dashboard_name.lower().replace(' ', '_')}")
            self._logger.info(f"Removed purchase price for player in dashboard {dashboard_name}")
        
        return removed
    
    def get_dashboard_profit_loss_stats(self, dashboard_name: str, owner_id: int) -> Dict[str, Any]:
        """Calculate profit/loss statistics for a dashboard"""
        dashboard = self.get_user_dashboard(dashboard_name, owner_id)
        if not dashboard:
            return {}
        
        total_invested = 0
        current_value = 0
        profit_loss = 0
        profitable_players = 0
        losing_players = 0
        players_with_purchase_price = 0
        
        for player_url in dashboard.player_urls:
            purchase_price = dashboard.purchase_prices.get(player_url)
            if purchase_price is None:
                continue
            
            players_with_purchase_price += 1
            total_invested += purchase_price
            
            # Get current price
            state = self.state_by_url.get(player_url)
            if state and state.price:
                current_value += state.price
                player_profit = state.price - purchase_price
                profit_loss += player_profit
                
                if player_profit > 0:
                    profitable_players += 1
                elif player_profit < 0:
                    losing_players += 1
        
        # Calculate percentages
        profit_loss_percent = 0
        if total_invested > 0:
            profit_loss_percent = (profit_loss / total_invested) * 100
        
        return {
            'total_invested': total_invested,
            'current_value': current_value,
            'profit_loss': profit_loss,
            'profit_loss_percent': profit_loss_percent,
            'profitable_players': profitable_players,
            'losing_players': losing_players,
            'neutral_players': players_with_purchase_price - profitable_players - losing_players,
            'players_with_purchase_price': players_with_purchase_price,
            'total_players': len(dashboard.player_urls)
        }