"""
Price fetching and parsing service for EA FC / FutBin / FUT.GG player data.
Supports multi-source resolution, concurrent price updates, Cloudflare bypass,
and fallback caching.
"""

import asyncio
import logging
import re
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Union
from bs4 import BeautifulSoup

from .http_client import HttpClient


# FUT rareType Mapping
RARE_TYPE_MAPPING = {
    0: "",               # Non-rare (Bronze/Silver)
    1: "",               # Gold Rare (Standard)
    3: "TOTW",           # Team of the Week
    5: "Hero",           # Hero Card
    6: "UCL RTTK",       # UEFA Road to the Knockouts
    11: "UCL",           # UEFA Champions League
    21: "Icon",          # Icon Card
    23: "FC Centurions", # Centurions
    41: "TOTY",          # Team of the Year
    42: "TOTY Icon",     # TOTY Icon
    50: "OTW",           # Ones to Watch
    60: "Trailblazers",  # Trailblazers
    71: "WC",            # World Cup
    160: "Icon",         # Icon (alternative code)
}

CLOUDFLARE_BLOCKED_TERMS = (
    'just a moment', 'attention required', 'cloudflare', 'security check',
    'access denied', 'turnstile', 'challenge-platform', 'robot check',
    'ddos-guard', '403 forbidden', 'error 403', '503 service', 'page not found'
)


@dataclass
class PlayerPrice:
    """Structured price data for a player"""
    price: Optional[int]
    image_url: Optional[str]
    name: Optional[str]
    raw_html: str
    success: bool
    error: Optional[str] = None
    card_version: str = ""
    is_stale: bool = False
    fetched_at: float = field(default_factory=time.time)


class PriceService:
    """Service for fetching, parsing, and caching EA FC player prices and sales"""

    def __init__(self, http_client: HttpClient, max_concurrent_requests: int = 3, platform: str = "pc"):
        self.http_client = http_client
        self.platform = (platform or "pc").lower()
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._logger = logging.getLogger(__name__)

        # Memory cache for stale-while-revalidate fallback: url -> PlayerPrice
        self._price_cache: Dict[str, PlayerPrice] = {}
        self._search_cache: Dict[str, List[Dict[str, Any]]] = {}

        # Backward compatibility alias
        self.fetch_price = self.fetch_player_price

        # Selectors for FutBin HTML parsing
        self.price_selectors = [
            '.price.inline-with-icon.lowest-price-1',
            '.price.lowest-price-1',
            '.lowest-price-1',
            '.price',
            '[class*="price"]'
        ]

        self.image_selectors = [
            '.playercard-27-base-img',
            '.playercard-26-base-img',
            '.playercard-base-img',
            '[class*="playercard"][class*="img"]',
            '[class*="playercard"][class*="base-img"]',
            '.player-img img',
            '.card-image img',
            'img.player-image'
        ]

        self.name_selectors = [
            '.comment-sidebar-button-title',
            '.playercard-27-name.text-ellipsis',
            '.playercard-27-name',
            '.playercard-26-name.text-ellipsis',
            '.playercard-26-name',
            'h1.player_name',
            '.player-header h1',
            '.player-name',
            'h1',
            '.card-name',
            '[class*="player"][class*="name"]'
        ]

    def _clean_query(self, query: str) -> str:
        """Sanitize query string"""
        return re.sub(r'\s+', ' ', (query or '').strip())

    async def search_player(self, query: str) -> List[Dict[str, Any]]:
        """Search for players by name on FutBin Search API with fallback to FUT.GG"""
        clean_q = self._clean_query(query)
        if not clean_q or len(clean_q) < 2:
            return []

        cache_key = clean_q.lower()
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]

        # 1. Primary: FutBin Search API
        results = await self._search_futbin(clean_q)
        if results:
            self._search_cache[cache_key] = results
            return results

        # 2. Secondary: FUT.GG Search / Lookup Fallback
        self._logger.info(f"FutBin search yielded no results for '{clean_q}', trying FUT.GG fallback...")
        results = await self._search_futgg(clean_q)
        if results:
            self._search_cache[cache_key] = results
            return results

        return []

    async def _search_futbin(self, query: str) -> List[Dict[str, Any]]:
        """Search FutBin JSON endpoint"""
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        url = f"https://www.futbin.com/players/search?query={encoded_query}&targetPage=PLAYER_PAGE"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://www.futbin.com/',
        }

        try:
            response = await self.http_client.get(url, headers=headers)
            if response.status != 200:
                self._logger.warning(f"FutBin search returned HTTP {response.status} for '{query}'")
                return []

            text = await response.text()
            if not text or not text.strip():
                return []

            clean_text = text.strip()
            # If FlareSolverr wrapped JSON in HTML <pre>...</pre> tags
            if '<pre' in clean_text:
                m_pre = re.search(r'<pre[^>]*>(.*?)</pre>', clean_text, re.DOTALL)
                if m_pre:
                    clean_text = m_pre.group(1).strip()
            elif clean_text.startswith('<'):
                m_json = re.search(r'(\[\s*\{.*?\}\s*\])', clean_text, re.DOTALL)
                if m_json:
                    clean_text = m_json.group(1).strip()
                else:
                    return []

            import html as html_lib
            clean_text = html_lib.unescape(clean_text)

            try:
                data = json.loads(clean_text)
            except Exception as je:
                self._logger.debug(f"JSON decode failed for search '{query}': {je}")
                return []

            if not isinstance(data, list):
                return []

            results = []
            for item in data:
                location = item.get('location', {})
                rel_url = location.get('url', '')
                if not rel_url:
                    continue
                full_url = f"https://www.futbin.com{rel_url}" if rel_url.startswith('/') else rel_url

                player_img = ""
                pimg_dict = item.get('playerImage', {}).get('fixed', {}).get('url', {})
                if isinstance(pimg_dict, dict):
                    player_img = pimg_dict.get('image1x', '')

                club_img = ""
                cimg_dict = item.get('clubImage', {}).get('fixed', {}).get('url', {}).get('day', {})
                if isinstance(cimg_dict, dict):
                    club_img = cimg_dict.get('image1x', '')

                rating = item.get('ratingSquare', {}).get('rating', '')
                raw_name = item.get('name', '')
                version = item.get('version', 'Normal')

                results.append({
                    'id': item.get('id'),
                    'name': raw_name,
                    'position': item.get('position', ''),
                    'version': version,
                    'rating': str(rating),
                    'url': full_url,
                    'image': player_img,
                    'club_image': club_img,
                    'source': 'futbin'
                })

            return results
        except Exception as e:
            self._logger.debug(f"FutBin search error for '{query}': {e}")
            return []

    async def _search_futgg(self, query: str) -> List[Dict[str, Any]]:
        """Fallback search using FUT.GG"""
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        url = f"https://www.fut.gg/players/?name={encoded_query}"

        try:
            response = await self.http_client.get(url)
            if response.status != 200:
                return []

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            results = []
            for s in soup.find_all('script'):
                stext = s.string or ''
                if 'ItemList' in stext and 'itemListElement' in stext:
                    try:
                        data = json.loads(stext)
                        for item_wrap in data.get('itemListElement', []):
                            item = item_wrap.get('item', {})
                            p_url = item.get('@id') or item.get('url', '')
                            p_name = item.get('name', '')
                            if p_url and p_name:
                                if not p_url.startswith('http'):
                                    p_url = f"https://www.fut.gg{p_url}"
                                results.append({
                                    'id': p_url.split('/')[-2] if '/' in p_url else '0',
                                    'name': p_name,
                                    'position': '',
                                    'version': 'Standard',
                                    'rating': '',
                                    'url': p_url,
                                    'image': '',
                                    'club_image': '',
                                    'source': 'futgg'
                                })
                    except Exception:
                        pass

            return results
        except Exception as e:
            self._logger.debug(f"FUT.GG fallback search error: {e}")
            return []

    async def resolve_player_url(self, query_or_url: str) -> Optional[Dict[str, Any]]:
        """
        Resolve any player URL (including outdated /26/, /25/ or slug) or query to the current FC 27 URL and metadata.
        """
        if not query_or_url:
            return None

        query = str(query_or_url).strip()
        if query.startswith(('http://', 'https://')):
            slug_match = re.search(r'/(?:player|sales)/\d+/([^/?#]+)', query)
            if slug_match:
                query = slug_match.group(1).replace('-', ' ')
            else:
                id_match = re.search(r'/(?:player|sales)/(\d+)', query)
                query = id_match.group(1) if id_match else ""

        if not query:
            return None

        try:
            results = await self.search_player(query)
            if results:
                clean_q = re.sub(r'[^a-zA-Z0-9]', '', query).lower()
                for r in results:
                    clean_name = re.sub(r'[^a-zA-Z0-9]', '', r.get('name', '')).lower()
                    if clean_q and (clean_q in clean_name or clean_name in clean_q):
                        return r
                return results[0]
        except Exception as e:
            self._logger.debug(f"Failed to resolve player URL for '{query_or_url}': {e}")

        return None

    async def fetch_player_price(self, url: str, retry_resolved: bool = True) -> PlayerPrice:
        """Fetch and parse a single player's price data with auto-resolution and sales/cache fallback"""
        async with self._semaphore:
            try:
                if "fut.gg" in url:
                    return await self._fetch_futgg_price(url)

                headers = {
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache'
                }

                target_url = url
                response = await self.http_client.get(target_url, headers=headers)
                html_content = await response.text()

                is_blocked = response.status in (403, 404, 503) or (html_content and any(t in html_content.lower() for t in CLOUDFLARE_BLOCKED_TERMS))

                # Auto-resolve outdated /26/, /25/ or blocked URLs
                if (not html_content or len(html_content) < 100 or is_blocked) and retry_resolved:
                    self._logger.info(f"Direct fetch for {target_url} status {response.status}. Attempting resolution...")
                    resolved = await self.resolve_player_url(url)
                    if resolved and resolved.get('url') and resolved['url'] != target_url:
                        res = await self.fetch_player_price(resolved['url'], retry_resolved=False)
                        if res.success:
                            self._price_cache[url] = res
                            return res

                # If direct player page is blocked or empty, try fetching live price from sales endpoint
                if (not html_content or len(html_content) < 100 or is_blocked):
                    try:
                        sales_res = await self.fetch_player_sales(target_url, retry_resolved=False)
                        if sales_res.get('success') and sales_res.get('sales'):
                            first_price = sales_res['sales'][0]['price']
                            p_name = sales_res.get('player_name') or "Spieler"
                            p_img = sales_res.get('image')
                            result = PlayerPrice(
                                price=first_price,
                                image_url=p_img,
                                name=p_name,
                                raw_html="",
                                success=True,
                                error=None,
                                card_version="",
                                is_stale=False,
                                fetched_at=time.time()
                            )
                            self._price_cache[url] = result
                            return result
                    except Exception as s_err:
                        self._logger.debug(f"Sales fallback on blocked player page failed: {s_err}")

                    if url in self._price_cache:
                        cached = self._price_cache[url]
                        return PlayerPrice(
                            price=cached.price,
                            image_url=cached.image_url,
                            name=cached.name,
                            raw_html="",
                            success=True,
                            error="Using cached price (temporary network limit)",
                            card_version=cached.card_version,
                            is_stale=True,
                            fetched_at=cached.fetched_at
                        )
                    return PlayerPrice(
                        price=None,
                        image_url=None,
                        name=None,
                        raw_html="",
                        success=False,
                        error=f"HTTP {response.status} from FutBin",
                        card_version=""
                    )

                parsed_data = self._parse_player_data(html_content)

                if parsed_data.get("price") is None and retry_resolved and ('/26/' in url or '/25/' in url):
                    resolved = await self.resolve_player_url(url)
                    if resolved and resolved.get('url') and resolved['url'] != target_url:
                        res = await self.fetch_player_price(resolved['url'], retry_resolved=False)
                        if res.success:
                            self._price_cache[url] = res
                            return res

                name = parsed_data.get("name")
                price = parsed_data.get("price")

                # Strategy 4: Fallback to fetch_player_sales if direct price is missing
                if price is None:
                    try:
                        sales_res = await self.fetch_player_sales(target_url, retry_resolved=False)
                        if sales_res.get('success') and sales_res.get('sales'):
                            price = sales_res['sales'][0]['price']
                            if not name and sales_res.get('player_name'):
                                name = sales_res['player_name']
                            if not parsed_data.get('image') and sales_res.get('image'):
                                parsed_data['image'] = sales_res['image']
                    except Exception as se:
                        self._logger.debug(f"Sales fallback in fetch_player_price failed: {se}")

                is_success = price is not None and bool(name)

                result = PlayerPrice(
                    price=price,
                    image_url=parsed_data.get("image"),
                    name=name,
                    raw_html=html_content,
                    success=is_success,
                    error=None if is_success else "Could not extract price or player name",
                    card_version=parsed_data.get("card_version", ""),
                    is_stale=False,
                    fetched_at=time.time()
                )

                if is_success:
                    self._price_cache[url] = result

                return result

            except Exception as e:
                self._logger.error(f"Failed to fetch price for {url}: {e}")
                if url in self._price_cache:
                    cached = self._price_cache[url]
                    return PlayerPrice(
                        price=cached.price,
                        image_url=cached.image_url,
                        name=cached.name,
                        raw_html="",
                        success=True,
                        error=f"Using cached price ({e})",
                        card_version=cached.card_version,
                        is_stale=True,
                        fetched_at=cached.fetched_at
                    )

                return PlayerPrice(
                    price=None,
                    image_url=None,
                    name=None,
                    raw_html="",
                    success=False,
                    error=str(e),
                    card_version=""
                )

    async def _fetch_futgg_price(self, url: str) -> PlayerPrice:
        """Parse player data and price from FUT.GG"""
        try:
            response = await self.http_client.get(url)
            if response.status != 200:
                return PlayerPrice(price=None, image_url=None, name=None, raw_html="", success=False, error=f"FUT.GG HTTP {response.status}")

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            name = None
            title_el = soup.select_one('title')
            if title_el:
                name_match = re.search(r'^(.*?)\s+(?:FC|EA FC|Rating)', title_el.get_text(strip=True))
                if name_match:
                    name = name_match.group(1).strip()

            img_url = None
            og_img = soup.select_one('meta[property="og:image"]')
            if og_img and og_img.get('content'):
                img_url = og_img['content']

            price = None
            for s in soup.find_all('script'):
                stext = s.string or ''
                if 'price:' in stext:
                    price_m = re.search(r'price:(\d{3,9})', stext)
                    if price_m:
                        p_val = int(price_m.group(1))
                        if 100 <= p_val <= 50_000_000:
                            price = p_val
                            break

            is_success = price is not None and bool(name)
            return PlayerPrice(
                price=price,
                image_url=img_url,
                name=name,
                raw_html=html,
                success=is_success,
                error=None if is_success else "Price not found on FUT.GG page"
            )
        except Exception as e:
            return PlayerPrice(price=None, image_url=None, name=None, raw_html="", success=False, error=str(e))

    async def fetch_multiple_prices(self, urls: List[str]) -> Dict[str, PlayerPrice]:
        """Fetch prices for multiple players concurrently"""
        if not urls:
            return {}

        self._logger.info(f"Fetching prices for {len(urls)} players")
        tasks = [self.fetch_player_price(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

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

        price_value = self._extract_price_from_soup(soup)
        img_url = self._extract_image_from_soup(soup)
        player_name = self._extract_name_from_soup(soup)
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

        for selector in self.price_selectors:
            elements = soup.select(selector)
            for element in elements:
                price = self._parse_price_text(element.get_text(strip=True))
                if price is not None:
                    return price

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
        """Extract player name, strictly avoiding Cloudflare challenge titles"""
        for selector in self.name_selectors:
            elements = soup.select(selector)
            for element in elements:
                name = element.get_text(strip=True)
                if name and len(name) > 1:
                    clean_name = re.sub(r'\s+EA\s+FC\s+\d+.*$', '', name, flags=re.I).strip()
                    if clean_name and not any(b in clean_name.lower() for b in CLOUDFLARE_BLOCKED_TERMS):
                        return clean_name

        title_element = soup.select_one('title')
        if title_element:
            title = title_element.get_text(strip=True)
            if any(b in title.lower() for b in CLOUDFLARE_BLOCKED_TERMS):
                return None
            if ' - ' in title:
                name_part = title.split(' - ')[0].strip()
                if not any(b in name_part.lower() for b in CLOUDFLARE_BLOCKED_TERMS):
                    return name_part
            clean_title = re.sub(r'\s+(?:EA\s+)?FC\s+\d+.*$', '', title, flags=re.I).strip()
            if clean_title and not any(b in clean_title.lower() for b in CLOUDFLARE_BLOCKED_TERMS):
                return clean_title

        return None

    def _extract_card_version_from_soup(self, soup: BeautifulSoup) -> str:
        """Extract card version from player page JSON script tags"""
        try:
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string and 'rareType' in script.string:
                    rare_type_match = re.search(r'"rareType"\s*:\s*(\d+)', script.string)
                    if rare_type_match:
                        rare_type_code = int(rare_type_match.group(1))
                        return RARE_TYPE_MAPPING.get(rare_type_code, "")
        except Exception as e:
            self._logger.debug(f"Failed to extract card version: {e}")
        return ""

    def _parse_price_text(self, text: str) -> Optional[int]:
        """Extract numerical price from text"""
        if not text:
            return None
        try:
            text = re.sub(r'\s+', '', text.strip())
            price_patterns = [
                r'(\d{1,3}(?:[,\.]\d{3})+)',
                r'(\d+)',
            ]
            for pattern in price_patterns:
                match = re.search(pattern, text)
                if match:
                    price_str = re.sub(r'[,\.]', '', match.group(1))
                    price_value = int(price_str)
                    if 100 <= price_value <= 50_000_000:
                        return price_value
            return None
        except Exception:
            return None

    def parse_alert_price(self, price_text: str) -> Optional[int]:
        """Parse price string for alerts (e.g. 32k -> 32000, 1.5m -> 1500000)"""
        if not price_text:
            return None
        price_text = price_text.strip().replace(' ', '').lower()
        try:
            if price_text.endswith('k'):
                base = price_text[:-1]
                multiplier = 1000
            elif price_text.endswith('m'):
                base = price_text[:-1]
                multiplier = 1_000_000
            else:
                base = price_text
                multiplier = 1

            if '.' in base:
                value = float(base) * multiplier
            else:
                value = int(base) * multiplier

            result = int(value)
            if 100 <= result <= 50_000_000:
                return result
        except Exception:
            pass
        return None

    async def fetch_player_sales(self, url_or_id: str, platform: Optional[str] = None, retry_resolved: bool = True) -> Dict[str, Any]:
        """
        Fetch the last sales for a player from FutBin (defaults to PC platform).
        """
        target_platform = (platform or getattr(self, 'platform', 'pc')).lower()
        if target_platform == "console":
            target_platform = "ps"

        if str(url_or_id).startswith(('http://', 'https://')):
            clean_url = re.sub(r'/market/?$', '', str(url_or_id))
            clean_url = re.sub(r'/sales/', '/player/', clean_url)
            clean_url = clean_url.split('?')[0]
            player_url = clean_url
            sales_url = re.sub(r'/player/', '/sales/', clean_url) + f"?platform={target_platform}"
        else:
            player_url = f"https://www.futbin.com/27/player/{url_or_id}"
            sales_url = f"https://www.futbin.com/27/sales/{url_or_id}?platform={target_platform}"

        self._logger.info(f"Fetching sales from {sales_url} (Player URL: {player_url})")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': player_url
        }

        sales_data = []
        avg_price = None
        player_name = None
        player_image = None
        error_msg = None

        # Strategy 1: Sales page Highcharts
        try:
            response = await self.http_client.get(sales_url, headers=headers)
            if response.status == 200:
                html_text = await response.text()
                try:
                    soup = BeautifulSoup(html_text, 'html.parser')
                    title_el = soup.select_one('title')
                    if title_el:
                        match_title = re.search(r'(?:EA FC \d+|FIFA \d+)?\s*(.*?)\s+Sales history', title_el.get_text(strip=True), re.I)
                        if match_title:
                            player_name = match_title.group(1).strip()
                except Exception:
                    pass

                marker = "highchartsStockChart("
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
                        except Exception:
                            pass
                        idx = brace_end + 1
                    else:
                        idx = brace_start + 1
            else:
                error_msg = f"HTTP {response.status}"
        except Exception as e:
            error_msg = str(e)

        # Strategy 2: Fallback to Player Page and recent prices
        if not sales_data:
            try:
                p_resp = await self.http_client.get(player_url, headers=headers)
                if p_resp.status == 200:
                    p_html = await p_resp.text()
                    p_soup = BeautifulSoup(p_html, 'html.parser')
                    if not player_name:
                        player_name = self._extract_name_from_soup(p_soup)
                    player_image = self._extract_image_from_soup(p_soup)

                    platform_class = "platform-pc-only" if target_platform == "pc" else "platform-ps-only"
                    box = p_soup.select_one(f'.price-box.{platform_class}') or p_soup.select_one(f'.{platform_class}')

                    prices = []
                    if box:
                        graph_el = box.select_one('[data-recent-prices]')
                        if graph_el and graph_el.get('data-recent-prices'):
                            raw_val = graph_el.get('data-recent-prices', '')
                            prices = [int(p.strip()) for p in raw_val.split(',') if p.strip().isdigit()]

                        if not prices:
                            lowest_wrapper = box.select_one('.lowest-prices-wrapper')
                            if lowest_wrapper:
                                for p_el in lowest_wrapper.find_all(class_='price'):
                                    txt = re.sub(r'[^\d]', '', p_el.get_text())
                                    if txt.isdigit():
                                        prices.append(int(txt))

                    if not prices:
                        m_any = re.search(r'data-recent-prices="([^"]+)"', p_html)
                        if m_any:
                            prices = [int(p.strip()) for p in m_any.group(1).split(',') if p.strip().isdigit()]

                    if prices:
                        if not avg_price:
                            avg_price = int(sum(prices) / len(prices))
                        for idx, pr in enumerate(prices[:10], 1):
                            trend_str = None
                            if idx < len(prices):
                                prev = prices[idx]
                                if prev > 0:
                                    diff = ((pr - prev) / prev) * 100
                                    trend_str = f"+{diff:.1f}%" if diff > 0 else f"{diff:.1f}%"
                            sales_data.append({
                                'date': f"Verkauf #{idx}",
                                'timestamp': None,
                                'price': pr,
                                'change': trend_str
                            })
                        error_msg = None
            except Exception as e:
                error_msg = str(e)

        # Strategy 3: Auto-resolution retry
        if not sales_data and retry_resolved:
            try:
                resolved = await self.resolve_player_url(url_or_id)
                if resolved and resolved.get('url') and resolved['url'] != player_url:
                    res = await self.fetch_player_sales(resolved['url'], platform=target_platform, retry_resolved=False)
                    if res.get('success'):
                        if not res.get('image') and resolved.get('image'):
                            res['image'] = resolved['image']
                        if not res.get('player_name') and resolved.get('name'):
                            res['player_name'] = resolved['name']
                        return res
            except Exception:
                pass

        return {
            'success': True if sales_data else False,
            'sales': sales_data,
            'avg_price': avg_price,
            'player_name': player_name,
            'image': player_image,
            'error': error_msg if not sales_data else None,
            'sales_url': sales_url,
            'platform': target_platform
        }

    async def fetch_squad_players(self, squad_url: str) -> List[Dict[str, Any]]:
        """Extract all players from a FutBin or FUT.GG squad URL"""
        try:
            response = await self.http_client.get(squad_url)
            if response.status != 200:
                self._logger.error(f"Squad URL returned status {response.status}")
                return []

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            players = []

            card_links = soup.find_all('a', href=re.compile(r'/(?:2[4-7]|player)/\d+/[^"\'\s]+'))
            seen_urls = set()

            for link in card_links:
                href = link.get('href', '')
                if not href.startswith('http'):
                    href = f"https://www.futbin.com{href}"
                clean_href = href.split('?')[0]
                if clean_href in seen_urls:
                    continue
                seen_urls.add(clean_href)

                name = link.get_text(strip=True) or "Spieler"
                players.append({
                    'name': name,
                    'url': clean_href,
                    'category': 'Squad'
                })

            if not players:
                for s in soup.find_all('script'):
                    stext = s.string or ''
                    if 'squad' in stext and 'players' in stext:
                        m_cards = re.findall(r'https?://www\.futbin\.com/\d+/player/\d+/[a-zA-Z0-9\-]+', stext)
                        for u in m_cards:
                            if u not in seen_urls:
                                seen_urls.add(u)
                                players.append({
                                    'name': u.split('/')[-1].replace('-', ' ').title(),
                                    'url': u,
                                    'category': 'Squad'
                                })

            return players
        except Exception as e:
            self._logger.error(f"Failed to fetch squad players from {squad_url}: {e}")
            return []

    async def find_player_for_query(self, query: str) -> Optional[Dict[str, Any]]:
        """Search and pick the best single matching player for a chat query"""
        results = await self.search_player(query)
        if not results:
            return None

        clean_q = re.sub(r'[^a-zA-Z0-9]', '', query).lower()
        for r in results:
            clean_name = re.sub(r'[^a-zA-Z0-9]', '', r.get('name', '')).lower()
            if clean_q == clean_name:
                return r

        for r in results:
            clean_name = re.sub(r'[^a-zA-Z0-9]', '', r.get('name', '')).lower()
            if clean_q in clean_name or clean_name in clean_q:
                return r

        return results[0]