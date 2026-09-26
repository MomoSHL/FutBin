"""
Price fetching and parsing service for FutBin player data.
Handles concurrent price updates with semaphore control and proper error handling.
"""

import asyncio
import logging
import re
import json
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from bs4 import BeautifulSoup

from .http_client import HttpClient


# FUTBin rareType Mapping (based on EA FC conventions)
RARE_TYPE_MAPPING = {
    0: "",              # Non-rare (Bronze/Silver)
    1: "",              # Gold Rare (Standard)
    3: "TOTW",          # Team of the Week
    5: "Hero",          # Hero Card
    6: "UCL RTTK",      # UEFA Road to the Knockouts
    11: "UCL",          # UEFA Champions League
    21: "Icon",         # Icon Card
    23: "FC Centurions",# Centurions
    41: "TOTY",         # Team of the Year
    42: "TOTY Icon",    # TOTY Icon
    50: "OTW",          # Ones to Watch
    60: "Trailblazers", # Trailblazers
    71: "WC",           # World Cup
    160: "Icon",        # Icon (alternative code)
    # Additional codes can be added as discovered
}


@dataclass
class PlayerPrice:
    """Structured price data for a player"""
    price: Optional[int]
    image_url: Optional[str]
    name: Optional[str]
    raw_html: str
    success: bool
    error: Optional[str] = None
    card_version: str = ""  # Event name (e.g., "TOTW", "Icon", "TOTY", etc.)


class PriceService:
    """Service for fetching and parsing FutBin player prices"""
    
    def __init__(self, http_client: HttpClient, max_concurrent_requests: int = 3, platform: str = "pc"):
        self.http_client = http_client
        self.platform = (platform or "pc").lower()
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._logger = logging.getLogger(__name__)
        
        # Alias for backward compatibility
        self.fetch_price = self.fetch_player_price
        
        # Price extraction selectors in order of preference
        self.price_selectors = [
            '.price.inline-with-icon.lowest-price-1',
            '.price.lowest-price-1',
            '.lowest-price-1',
            '.price',
            '[class*="price"]'
        ]
        
        # Image extraction selectors
        self.image_selectors = [
            '.playercard-26-base-img',
            '.playercard-base-img',
            '[class*="playercard"][class*="img"]',
            '.player-img img',
            '.card-image img'
        ]
        
        # Name extraction selectors
        self.name_selectors = [
            '.playercard-26-name.text-ellipsis',
            '.playercard-26-name', 
            'h1.player_name',
            '.player-header h1',
            '.player-name',
            'h1',
            '.card-name',
            '[class*="player"][class*="name"]'
        ]

    async def fetch_player_price(self, url: str) -> PlayerPrice:
        """Fetch and parse a single player's price data"""
        async with self._semaphore:
            try:
                # Fetch HTML content
                headers = {
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache'
                }
                
                response = await self.http_client.get(url, headers=headers)
                html_content = await response.text()
                
                if not html_content or len(html_content) < 100:
                    return PlayerPrice(
                        price=None, 
                        image_url=None, 
                        name=None, 
                        raw_html="", 
                        success=False,
                        error="Empty or too short HTML response",
                        card_version=""
                    )
                
                # Parse the content
                parsed_data = self._parse_player_data(html_content)
                
                return PlayerPrice(
                    price=parsed_data.get("price"),
                    image_url=parsed_data.get("image"),
                    name=parsed_data.get("name"),
                    raw_html=html_content,
                    success=True,
                    card_version=parsed_data.get("card_version", "")
                )
                
            except Exception as e:
                self._logger.error(f"Failed to fetch price for {url}: {e}")
                return PlayerPrice(
                    price=None,
                    image_url=None,
                    name=None,
                    raw_html="",
                    success=False,
                    error=str(e),
                    card_version=""
                )

    async def fetch_multiple_prices(self, urls: List[str]) -> Dict[str, PlayerPrice]:
        """Fetch prices for multiple players concurrently"""
        if not urls:
            return {}
        
        self._logger.info(f"Fetching prices for {len(urls)} players")
        
        # Create tasks for all URLs
        tasks = [self.fetch_player_price(url) for url in urls]
        
        # Execute concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Build result dictionary
        price_data = {}
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                self._logger.error(f"Exception fetching {url}: {result}")
                price_data[url] = PlayerPrice(
                    price=None,
                    image_url=None,
                    name=None,
                    raw_html="",
                    success=False,
                    error=str(result),
                    card_version=""
                )
            else:
                price_data[url] = result
        
        successful_fetches = sum(1 for p in price_data.values() if p.success)
        self._logger.info(f"Price fetch completed: {successful_fetches}/{len(urls)} successful")
        
        return price_data

    def _parse_player_data(self, html: str) -> Dict[str, Any]:
        """Extract price, image URL, player name, and card version from HTML"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception as e:
            self._logger.error(f"Failed to parse HTML: {e}")
            return {"price": None, "image": None, "name": None, "card_version": ""}
        
        # Extract price
        price_value = self._extract_price_from_soup(soup)
        
        # Extract image URL
        img_url = self._extract_image_from_soup(soup)
        
        # Extract player name
        player_name = self._extract_name_from_soup(soup)
        
        # Extract card version from JSON data in script tags
        card_version = self._extract_card_version_from_soup(soup)
        
        return {
            "price": price_value, 
            "image": img_url,
            "name": player_name,
            "card_version": card_version
        }

    def _extract_price_from_soup(self, soup: BeautifulSoup) -> Optional[int]:
        """Extract price using platform-specific container first, then fallback selectors"""
        platform = getattr(self, 'platform', 'pc').lower()
        
        # 1. Platform-specific container search (e.g. .platform-pc-only vs .platform-ps-only)
        platform_class = "platform-pc-only" if platform == "pc" else "platform-ps-only"
        platform_box = soup.select_one(f'.{platform_class}')
        if platform_box:
            for selector in self.price_selectors:
                elements = platform_box.select(selector)
                for element in elements:
                    price = self._parse_price_text(element.get_text(strip=True))
                    if price is not None:
                        return price
            wrapper = platform_box.select_one('.lowest-prices-wrapper')
            if wrapper:
                price = self._parse_price_text(wrapper.get_text(strip=True))
                if price is not None:
                    return price

        # 2. Try standard selectors as fallback
        for selector in self.price_selectors:
            elements = soup.select(selector)
            for element in elements:
                price = self._parse_price_text(element.get_text(strip=True))
                if price is not None:
                    return price
        
        # Fallback: Search in lowest-prices-wrapper
        wrapper = soup.select_one('.lowest-prices-wrapper')
        if wrapper:
            price_text = wrapper.get_text(strip=True)
            price = self._parse_price_text(price_text)
            if price is not None:
                return price
        
        return None

    def _extract_image_from_soup(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract image URL using multiple selector strategies"""
        for selector in self.image_selectors:
            elements = soup.select(selector)
            for element in elements:
                img_src = element.get('src') or element.get('data-src')
                if img_src and img_src.startswith(('http', '//')):
                    if img_src.startswith('//'):
                        img_src = 'https:' + img_src
                    return img_src
        return None

    def _extract_name_from_soup(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract player name using multiple selector strategies"""
        for selector in self.name_selectors:
            elements = soup.select(selector)
            for element in elements:
                name = element.get_text(strip=True)
                if name and len(name) > 1:
                    return name
        
        # Fallback: Extract from title
        title_element = soup.select_one('title')
        if title_element:
            title = title_element.get_text(strip=True)
            # Common title patterns: "Player Name - FC 26 Player Prices - FUTBIN"
            if ' - ' in title:
                return title.split(' - ')[0].strip()
        
        return None

    def _extract_card_version_from_soup(self, soup: BeautifulSoup) -> str:
        """Extract card version/event name from player page JSON data"""
        try:
            # Look for player data in script tags (similar to squad parser)
            scripts = soup.find_all('script')
            
            for script in scripts:
                if script.string and 'rareType' in script.string:
                    script_content = script.string.strip()
                    
                    # Try to find rareType in JSON structure
                    # Look for patterns like "rareType":160 or "rareType": 1
                    rare_type_match = re.search(r'"rareType"\s*:\s*(\d+)', script_content)
                    
                    if rare_type_match:
                        rare_type_code = int(rare_type_match.group(1))
                        card_version = RARE_TYPE_MAPPING.get(rare_type_code, "")
                        
                        if card_version:
                            self._logger.debug(f"Found card version: {card_version} (rareType: {rare_type_code})")
                        
                        return card_version
                        
        except Exception as e:
            self._logger.debug(f"Failed to extract card version: {e}")
        
        return ""

    def _parse_price_text(self, text: str) -> Optional[int]:
        """Extract numerical price from text with improved regex"""
        if not text:
            return None
        
        try:
            # Remove whitespace and normalize
            text = re.sub(r'\s+', '', text.strip())
            
            # Look for price patterns like "1,234,567", "1.234.567", "1234567"
            price_patterns = [
                r'(\d{1,3}(?:[,\.]\d{3})+)',  # 1,234,567 or 1.234.567
                r'(\d+)',  # Simple number
            ]
            
            for pattern in price_patterns:
                match = re.search(pattern, text)
                if match:
                    price_str = match.group(1)
                    # Remove separators and convert
                    price_str = re.sub(r'[,\.]', '', price_str)
                    price_value = int(price_str)
                    
                    # Reasonable price range check (100 - 50M coins)
                    if 100 <= price_value <= 50_000_000:
                        return price_value
            
            return None
            
        except (ValueError, AttributeError) as e:
            self._logger.debug(f"Failed to parse price from '{text}': {e}")
            return None

    def parse_alert_price(self, price_text: str) -> Optional[int]:
        """Parse price text for alerts (e.g. 32k -> 32000, 1.5m -> 1500000)"""
        if not price_text:
            return None
        
        # Clean input
        price_text = price_text.strip().replace(' ', '').lower()
        
        try:
            # Handle k/m suffixes
            if price_text.endswith('k'):
                base = price_text[:-1]
                multiplier = 1000
            elif price_text.endswith('m'):
                base = price_text[:-1]
                multiplier = 1_000_000
            else:
                base = price_text
                multiplier = 1
            
            # Parse the base number (can be float for m values like 1.5m)
            if '.' in base:
                value = float(base) * multiplier
            else:
                value = int(base) * multiplier
            
            # Convert to int and validate range
            result = int(value)
            if 100 <= result <= 50_000_000:
                return result
                
        except (ValueError, AttributeError) as e:
            self._logger.debug(f"Failed to parse alert price from '{price_text}': {e}")
            
        return None

    async def search_player(self, query: str) -> List[Dict[str, Any]]:
        """Search for players by name on Futbin"""
        if not query or len(query.strip()) < 2:
            return []
            
        import urllib.parse
        encoded_query = urllib.parse.quote(query.strip())
        url = f"https://www.futbin.com/players/search?query={encoded_query}&targetPage=PLAYER_PAGE"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*'
        }
        
        try:
            response = await self.http_client.get(url, headers=headers)
            data = await response.json(content_type=None)
            if not isinstance(data, list):
                return []
                
            results = []
            for item in data:
                location = item.get('location', {})
                rel_url = location.get('url', '')
                if not rel_url:
                    continue
                full_url = f"https://www.futbin.com{rel_url}" if rel_url.startswith('/') else rel_url
                
                # Player image
                player_img = ""
                pimg_dict = item.get('playerImage', {}).get('fixed', {}).get('url', {})
                if isinstance(pimg_dict, dict):
                    player_img = pimg_dict.get('image1x', '')
                
                # Club image
                club_img = ""
                cimg_dict = item.get('clubImage', {}).get('fixed', {}).get('url', {}).get('day', {})
                if isinstance(cimg_dict, dict):
                    club_img = cimg_dict.get('image1x', '')
                    
                rating = item.get('ratingSquare', {}).get('rating', '')
                
                results.append({
                    'id': item.get('id'),
                    'name': item.get('name', ''),
                    'position': item.get('position', ''),
                    'version': item.get('version', 'Normal'),
                    'rating': str(rating),
                    'url': full_url,
                    'image': player_img,
                    'club_image': club_img
                })
            return results
        except Exception as e:
            self._logger.error(f"Failed to search players for '{query}': {e}")
            return []

    async def fetch_player_sales(self, url_or_id: str, platform: Optional[str] = None) -> Dict[str, Any]:
        """Fetch the last sales for a player from Futbin (defaults to PC platform)"""
        target_platform = (platform or getattr(self, 'platform', 'pc')).lower()
        if target_platform == "console":
            target_platform = "ps"
            
        # Build sales URL
        if str(url_or_id).startswith(('http://', 'https://')):
            clean_url = re.sub(r'/market/?$', '', str(url_or_id))
            clean_url = re.sub(r'/player/', '/sales/', clean_url)
            clean_url = clean_url.split('?')[0]
            sales_url = f"{clean_url}?platform={target_platform}"
        else:
            sales_url = f"https://www.futbin.com/26/sales/{url_or_id}?platform={target_platform}"
            
        self._logger.info(f"Fetching sales from {sales_url}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        
        try:
            response = await self.http_client.get(sales_url, headers=headers)
            html_text = await response.text()
            
            if not html_text or len(html_text) < 200:
                return {
                    'success': False,
                    'sales': [],
                    'error': 'Leere Antwort von Futbin erhalten',
                    'sales_url': sales_url,
                    'platform': target_platform
                }
            
            marker = "highchartsStockChart("
            sales_data = []
            avg_price = None
            player_name = None
            
            # Extract player name from page title if possible
            try:
                soup = BeautifulSoup(html_text, 'html.parser')
                title_el = soup.select_one('title')
                if title_el:
                    title_text = title_el.get_text(strip=True)
                    match_title = re.search(r'(?:EA FC \d+|FIFA \d+)?\s*(.*?)\s+Sales history', title_text, re.I)
                    if match_title:
                        player_name = match_title.group(1).strip()
            except Exception:
                pass
                
            idx = 0
            while True:
                pos = html_text.find(marker, idx)
                if pos == -1:
                    break
                comma_pos = html_text.find(",", pos + len(marker))
                if comma_pos == -1:
                    idx = pos + len(marker)
                    continue
                brace_start = html_text.find("{", comma_pos)
                if brace_start == -1:
                    idx = comma_pos + 1
                    continue
                
                count = 0
                in_string = False
                escape = False
                brace_end = -1
                for i in range(brace_start, len(html_text)):
                    c = html_text[i]
                    if escape:
                        escape = False
                        continue
                    if c == '\\':
                        escape = True
                        continue
                    if c == '"':
                        in_string = not in_string
                        continue
                    if not in_string:
                        if c == '{':
                            count += 1
                        elif c == '}':
                            count -= 1
                            if count == 0:
                                brace_end = i
                                break
                                
                if brace_end != -1:
                    json_str = html_text[brace_start:brace_end+1]
                    try:
                        chart_data = json.loads(json_str)
                        chart_title = chart_data.get("title", "")
                        if "sales" in chart_title.lower() or "live" in chart_title.lower():
                            avg_price = chart_data.get("avg")
                            series = chart_data.get("series", [])
                            if series and "data" in series[0]:
                                items = series[0]["data"]
                                # In Highcharts data, the most recent sales are at the end (chronological order)
                                # Take the last 10 items and reverse so the newest is first
                                for item in reversed(items[-10:]):
                                    change_val = None
                                    if isinstance(item.get("change"), dict):
                                        change_val = item["change"].get("valueString")
                                    sales_data.append({
                                        'date': item.get("name"),
                                        'timestamp': item.get("x"),
                                        'price': item.get("y"),
                                        'change': change_val
                                    })
                                break
                    except Exception as e:
                        self._logger.debug(f"JSON parse error in highcharts: {e}")
                    idx = brace_end + 1
                else:
                    idx = brace_start + 1
                    
            return {
                'success': True,
                'sales': sales_data,
                'avg_price': avg_price,
                'player_name': player_name,
                'sales_url': sales_url,
                'platform': target_platform
            }
            
        except Exception as e:
            self._logger.error(f"Failed to fetch player sales from {sales_url}: {e}")
            return {
                'success': False,
                'sales': [],
                'error': str(e),
                'sales_url': sales_url,
                'platform': target_platform
            }