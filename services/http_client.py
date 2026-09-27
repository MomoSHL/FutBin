"""
HTTP client service with Cloudflare bypass, FlareSolverr integration,
browser profile rotation, and connection pooling.
"""

import asyncio
import logging
import random
import os
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union, Dict

import aiohttp
import requests

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False


@dataclass(slots=True)
class RetryConfig:
    attempts: int = 3
    backoff_base: float = 0.75
    backoff_max: float = 6.0
    jitter: float = 0.35


class AdaptedResponse:
    """Wrapper around requests or curl_cffi Response providing an aiohttp-compatible async interface."""

    def __init__(self, response: Any, custom_text: Optional[str] = None, custom_status: Optional[int] = None):
        self._resp = response
        self._custom_text = custom_text
        self.status = custom_status if custom_status is not None else getattr(response, 'status_code', 200)
        self.status_code = self.status
        self.headers = getattr(response, 'headers', {})

    async def text(self, encoding: Optional[str] = None) -> str:
        if self._custom_text is not None:
            return self._custom_text
        if encoding and hasattr(self._resp, 'encoding'):
            self._resp.encoding = encoding
        elif hasattr(self._resp, 'encoding') and (not self._resp.encoding or self._resp.encoding.lower() == 'iso-8859-1'):
            self._resp.encoding = 'utf-8'
        return getattr(self._resp, 'text', '')

    async def json(self, *, content_type: Optional[str] = None, **kwargs) -> Any:
        if self._custom_text is not None:
            return json.loads(self._custom_text)
        if hasattr(self._resp, 'json'):
            return self._resp.json(**kwargs)
        txt = await self.text()
        return json.loads(txt)

    async def read(self) -> bytes:
        if self._custom_text is not None:
            return self._custom_text.encode('utf-8')
        return getattr(self._resp, 'content', b'')

    def raise_for_status(self) -> None:
        if hasattr(self._resp, 'raise_for_status'):
            self._resp.raise_for_status()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class HttpClient:
    """Shared HTTP client with semaphore-guarded execution, retry logic, FlareSolverr, and Cloudflare bypass."""

    CLOUDFLARE_MARKERS = (
        'just a moment',
        'attention required! | cloudflare',
        'checking your browser',
        'turnstile',
        'cf-browser-verification',
        'challenge-running',
        'enable javascript and cookies to continue'
    )

    def __init__(
        self,
        *,
        default_headers: Optional[dict[str, str]] = None,
        timeout: float = 14.0,
        max_concurrency: int = 4,
        retry_config: RetryConfig | None = None,
    ) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._requests_session: Any = None
        self._timeout = timeout
        self._default_headers = default_headers or {}
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._retry_config = retry_config or RetryConfig()
        self._closing = False
        self._logger = logging.getLogger(__name__)
        
        # FlareSolverr cookies & User-Agent cache for fast follow-up requests
        self._cached_cookies: Dict[str, str] = {}
        self._cached_user_agent: Optional[str] = None
        self._flaresolverr_active = False

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HttpClient session not started. Call start() first.")
        return self._session

    async def start(self) -> None:
        if self._session is not None:
            return
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        connector = aiohttp.TCPConnector(limit=None, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers=self._default_headers,
            connector=connector,
        )
        if HAS_CURL_CFFI:
            self._requests_session = cffi_requests.Session(impersonate="chrome131")
            self._logger.info("HTTP client started with curl_cffi (Chrome 131 impersonation enabled)")
        else:
            self._requests_session = requests.Session()
            self._logger.warning("HTTP client started with standard requests (curl_cffi is NOT available!)")

        # Check if FlareSolverr is reachable
        await self._check_flaresolverr_availability()

    async def _check_flaresolverr_availability(self) -> None:
        """Check whether FlareSolverr is configured and reachable"""
        candidates = [
            os.getenv('FLARESOLVERR_URL'),
            'http://flaresolverr:8191/v1',
            'http://127.0.0.1:8191/v1',
            'http://localhost:8191/v1'
        ]
        valid_url = None
        for u in candidates:
            if not u:
                continue
            try:
                def _test():
                    return requests.get(u.replace('/v1', '/'), timeout=2.0)
                resp = await asyncio.to_thread(_test)
                if resp.status_code in (200, 404, 405):
                    valid_url = u
                    break
            except Exception:
                continue

        if valid_url:
            self._flaresolverr_active = True
            os.environ['FLARESOLVERR_URL'] = valid_url
            self._logger.info(f"FlareSolverr detected and active at {valid_url}")
        else:
            self._logger.debug("FlareSolverr not detected on default endpoints. Direct / curl_cffi mode active.")

    async def close(self) -> None:
        self._closing = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
        if self._requests_session is not None:
            self._requests_session.close()
            self._requests_session = None
        self._session = None
        self._logger.info("HTTP client session closed")

    def _is_cloudflare_challenge(self, status: int, text: str) -> bool:
        """Check if response is a Cloudflare challenge page"""
        if status in (403, 503):
            return True
        if not text:
            return False
        lower = text.lower()
        return any(marker in lower for marker in self.CLOUDFLARE_MARKERS)

    def _sync_flaresolverr_request(self, url: str) -> Optional[AdaptedResponse]:
        """Try resolving challenge via FlareSolverr if configured"""
        flaresolverr_url = os.getenv('FLARESOLVERR_URL')
        if not flaresolverr_url and not self._flaresolverr_active:
            return None

        fs_endpoint = flaresolverr_url or 'http://flaresolverr:8191/v1'
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": int(self._timeout * 1000)
            }
            if self._cached_cookies:
                payload["cookies"] = [
                    {"name": k, "value": v, "domain": ".futbin.com"}
                    for k, v in self._cached_cookies.items()
                ]

            resp = requests.post(
                fs_endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._timeout + 6
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    solution = data.get("solution", {})
                    response_text = solution.get("response", "")
                    status = solution.get("status", 200)
                    
                    cookies_list = solution.get("cookies", [])
                    for cookie in cookies_list:
                        name = cookie.get("name")
                        value = cookie.get("value")
                        if name and value:
                            self._cached_cookies[name] = value

                    user_agent = solution.get("userAgent")
                    if user_agent:
                        self._cached_user_agent = user_agent

                    self._logger.info(f"Cloudflare challenge successfully solved via FlareSolverr for {url}")
                    return AdaptedResponse(resp, custom_text=response_text, custom_status=status)
                else:
                    self._logger.warning(f"FlareSolverr returned non-ok status: {data.get('message')}")
        except Exception as e:
            self._logger.debug(f"FlareSolverr request failed for {url}: {e}")
        return None

    def _sync_request(self, method: str, url: str, headers: Optional[dict[str, str]] = None) -> AdaptedResponse:
        proxy = os.getenv('FUTBIN_PROXY') or os.getenv('MARKET_PROXY') or os.getenv('HTTP_PROXY') or os.getenv('HTTPS_PROXY')
        proxies = {"http": proxy, "https": proxy} if proxy else None

        # Check if URL is an API/XHR call
        is_xhr = "search" in url or (headers and "json" in str(headers.get("Accept", "")).lower())

        if HAS_CURL_CFFI:
            if is_xhr:
                req_headers = {
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': 'https://www.futbin.com/',
                    'Origin': 'https://www.futbin.com',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                }
            else:
                req_headers = {
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Referer': 'https://www.futbin.com/',
                }

            if self._cached_user_agent:
                req_headers['User-Agent'] = self._cached_user_agent

            if self._default_headers:
                req_headers.update(self._default_headers)
            if headers:
                req_headers.update(headers)

            if not self._cached_user_agent:
                for k in list(req_headers.keys()):
                    if k.lower() in ('user-agent', 'sec-ch-ua', 'sec-ch-ua-platform', 'sec-ch-ua-mobile', 'host'):
                        del req_headers[k]

            sess = self._requests_session
            if sess is None:
                sess = cffi_requests.Session(impersonate="chrome131")
                self._requests_session = sess

            if self._cached_cookies:
                for k, v in self._cached_cookies.items():
                    sess.cookies.set(k, v, domain=".futbin.com")

            last_resp = None
            try:
                resp = sess.request(method, url, headers=req_headers, timeout=self._timeout, proxies=proxies)
                body_text = resp.text if hasattr(resp, 'text') else ''
                if resp.status_code == 200 and not self._is_cloudflare_challenge(resp.status_code, body_text):
                    return AdaptedResponse(resp)
                last_resp = resp
            except Exception as e:
                self._logger.debug(f"Request failed for {url} with chrome131 ({e}). Trying fallback profiles...")

            # Fallback browser profiles
            for fallback_imp in ["chrome124", "safari180", "firefox135", "edge101"]:
                try:
                    f_sess = cffi_requests.Session(impersonate=fallback_imp)
                    if self._cached_cookies:
                        for k, v in self._cached_cookies.items():
                            f_sess.cookies.set(k, v, domain=".futbin.com")
                    resp = f_sess.request(method, url, headers=req_headers, timeout=self._timeout, proxies=proxies)
                    body_text = resp.text if hasattr(resp, 'text') else ''
                    if resp.status_code == 200 and not self._is_cloudflare_challenge(resp.status_code, body_text):
                        self._logger.info(f"Successfully fetched {url} using {fallback_imp} (HTTP 200)")
                        self._requests_session = f_sess
                        return AdaptedResponse(resp)
                    last_resp = resp
                except Exception as fe:
                    self._logger.debug(f"Fallback {fallback_imp} error for {url}: {fe}")

            # Try FlareSolverr if direct requests were challenged / blocked
            fs_resp = self._sync_flaresolverr_request(url)
            if fs_resp and fs_resp.status == 200:
                return fs_resp

            # Try standard requests as final fallback
            try:
                std_headers = {
                    'User-Agent': self._cached_user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
                    'Accept': 'application/json, text/plain, */*' if is_xhr else 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Referer': 'https://www.futbin.com/',
                }
                if headers:
                    std_headers.update(headers)
                std_sess = requests.Session()
                if self._cached_cookies:
                    std_sess.cookies.update(self._cached_cookies)
                std_resp = std_sess.request(method, url, headers=std_headers, timeout=self._timeout, proxies=proxies)
                body_text = std_resp.text if hasattr(std_resp, 'text') else ''
                if std_resp.status_code == 200 and not self._is_cloudflare_challenge(std_resp.status_code, body_text):
                    self._logger.info(f"Successfully fetched {url} using standard requests fallback (HTTP 200)")
                    return AdaptedResponse(std_resp)
                if not last_resp:
                    last_resp = std_resp
            except Exception as std_e:
                self._logger.debug(f"Standard requests fallback error for {url}: {std_e}")

            return AdaptedResponse(last_resp) if last_resp else AdaptedResponse(None, custom_text="", custom_status=503)

        else:
            merged_headers = {
                'User-Agent': self._cached_user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*' if is_xhr else 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
                'Referer': 'https://www.futbin.com/',
            }
            if self._default_headers:
                merged_headers.update(self._default_headers)
            if headers:
                merged_headers.update(headers)

            sess = self._requests_session or requests.Session()
            if self._cached_cookies:
                sess.cookies.update(self._cached_cookies)
            try:
                resp = sess.request(method, url, headers=merged_headers, timeout=self._timeout, proxies=proxies)
                body_text = resp.text if hasattr(resp, 'text') else ''
                if resp.status_code == 200 and not self._is_cloudflare_challenge(resp.status_code, body_text):
                    return AdaptedResponse(resp)
            except Exception as e:
                self._logger.debug(f"Sync request failed for {url}: {e}")

            # Try FlareSolverr
            fs_resp = self._sync_flaresolverr_request(url)
            if fs_resp and fs_resp.status == 200:
                return fs_resp

            return AdaptedResponse(resp if 'resp' in locals() else None, custom_text="", custom_status=503)

    async def _with_retry(
        self,
        func: Callable[[], Awaitable[Any]],
    ) -> Any:
        cfg = self._retry_config
        last_exc: Exception | None = None
        for attempt in range(1, cfg.attempts + 1):
            try:
                response = await func()
                if response.status >= 500:
                    raise aiohttp.ClientResponseError(
                        request_info=getattr(response, 'request_info', None),
                        history=getattr(response, 'history', ()),
                        status=response.status,
                        message=f"HTTP {response.status} server error",
                        headers=response.headers,
                    )
                return response
            except (aiohttp.ClientError, asyncio.TimeoutError, requests.RequestException) as exc:
                last_exc = exc
                if attempt >= cfg.attempts or self._closing:
                    break
                backoff = min(cfg.backoff_base * (2 ** (attempt - 1)), cfg.backoff_max)
                backoff += random.uniform(0, cfg.jitter)
                self._logger.warning(
                    "HTTP request failed (attempt %s/%s): %s. Backing off %.2fs",
                    attempt,
                    cfg.attempts,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
        if last_exc:
            raise last_exc
        raise RuntimeError("HTTP request failed without exception")

    async def get_json(self, url: str, *, headers: Optional[dict[str, str]] = None) -> Any:
        response = await self.get(url, headers=headers)
        return await response.json(content_type=None)

    async def get_text(self, url: str, *, headers: Optional[dict[str, str]] = None) -> str:
        response = await self.get(url, headers=headers)
        return await response.text()

    async def get(self, url: str, *, headers: Optional[dict[str, str]] = None) -> Union[aiohttp.ClientResponse, AdaptedResponse]:
        async with self._semaphore:
            if "futbin.com" in url or "fut.gg" in url:
                async def _do_sync_get() -> AdaptedResponse:
                    return await asyncio.to_thread(self._sync_request, "GET", url, headers)
                return await self._with_retry(_do_sync_get)

            session = self.session

            async def _do_request() -> aiohttp.ClientResponse:
                return await session.get(url, headers=headers)

            try:
                response = await self._with_retry(_do_request)
                if response.status == 403:
                    return await asyncio.to_thread(self._sync_request, "GET", url, headers)
                return response
            except Exception as exc:
                self._logger.warning("aiohttp request failed (%s), attempting requests fallback", exc)
                return await asyncio.to_thread(self._sync_request, "GET", url, headers)

    async def head(self, url: str, *, headers: Optional[dict[str, str]] = None) -> Union[aiohttp.ClientResponse, AdaptedResponse]:
        async with self._semaphore:
            if "futbin.com" in url or "fut.gg" in url:
                async def _do_sync_head() -> AdaptedResponse:
                    return await asyncio.to_thread(self._sync_request, "HEAD", url, headers)
                return await self._with_retry(_do_sync_head)

            session = self.session

            async def _do_request() -> aiohttp.ClientResponse:
                return await session.head(url, headers=headers)

            return await self._with_retry(_do_request)
