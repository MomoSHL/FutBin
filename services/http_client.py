import asyncio
import logging
import random
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

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

    def __init__(self, response: Any):
        self._resp = response
        self.status = getattr(response, 'status_code', 200)
        self.status_code = self.status
        self.headers = getattr(response, 'headers', {})

    async def text(self, encoding: Optional[str] = None) -> str:
        if encoding:
            self._resp.encoding = encoding
        elif hasattr(self._resp, 'encoding') and (not self._resp.encoding or self._resp.encoding.lower() == 'iso-8859-1'):
            self._resp.encoding = 'utf-8'
        return self._resp.text

    async def json(self, *, content_type: Optional[str] = None, **kwargs) -> Any:
        return self._resp.json(**kwargs)

    async def read(self) -> bytes:
        return self._resp.content

    def raise_for_status(self) -> None:
        self._resp.raise_for_status()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class HttpClient:
    """Shared HTTP client with semaphore-guarded execution, retry logic, and Cloudflare bypass."""

    def __init__(
        self,
        *,
        default_headers: Optional[dict[str, str]] = None,
        timeout: float = 10.0,
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
            self._requests_session = cffi_requests.Session(impersonate="chrome124")
            self._logger.info("HTTP client started with curl_cffi (Chrome impersonation enabled)")
        else:
            self._requests_session = requests.Session()
            self._logger.info("HTTP client started with standard requests")

    async def close(self) -> None:
        self._closing = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
        if self._requests_session is not None:
            self._requests_session.close()
            self._requests_session = None
        self._session = None
        self._logger.info("HTTP client session closed")

    def _sync_request(self, method: str, url: str, headers: Optional[dict[str, str]] = None) -> AdaptedResponse:
        merged_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://www.futbin.com/',
        }
        if self._default_headers:
            merged_headers.update(self._default_headers)
        if headers:
            merged_headers.update(headers)
            
        if self._requests_session is not None:
            sess = self._requests_session
        elif HAS_CURL_CFFI:
            sess = cffi_requests.Session(impersonate="chrome124")
        else:
            sess = requests.Session()
            
        resp = sess.request(method, url, headers=merged_headers, timeout=self._timeout)
        return AdaptedResponse(resp)

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
            # FutBin is protected by Cloudflare which blocks aiohttp's TLS fingerprint.
            # Route requests to futbin.com via requests in a thread pool to avoid 403 blocks.
            if "futbin.com" in url:
                async def _do_sync_get() -> AdaptedResponse:
                    return await asyncio.to_thread(self._sync_request, "GET", url, headers)
                return await self._with_retry(_do_sync_get)

            session = self.session

            async def _do_request() -> aiohttp.ClientResponse:
                return await session.get(url, headers=headers)

            try:
                response = await self._with_retry(_do_request)
                if response.status == 403:
                    # Cloudflare 403 fallback
                    return await asyncio.to_thread(self._sync_request, "GET", url, headers)
                return response
            except Exception as exc:
                self._logger.warning("aiohttp request failed (%s), attempting requests fallback", exc)
                return await asyncio.to_thread(self._sync_request, "GET", url, headers)

    async def head(self, url: str, *, headers: Optional[dict[str, str]] = None) -> Union[aiohttp.ClientResponse, AdaptedResponse]:
        async with self._semaphore:
            if "futbin.com" in url:
                async def _do_sync_head() -> AdaptedResponse:
                    return await asyncio.to_thread(self._sync_request, "HEAD", url, headers)
                return await self._with_retry(_do_sync_head)

            session = self.session

            async def _do_request() -> aiohttp.ClientResponse:
                return await session.head(url, headers=headers)

            return await self._with_retry(_do_request)
