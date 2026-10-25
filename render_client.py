"""Plugin-owned, deadline-bounded AstrBot text-to-image client.

Keeps the AstrBot 4.26.x /generate payload and returned image URL contract.
The plugin owns endpoint ordering and health; the framework renderer remains
a strictly time-bounded compatibility fallback.
"""
import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

import aiohttp

OFFICIAL_POOL_URL = "https://api.soulter.top/astrbot/t2i-endpoints"
DEFAULT_ENDPOINT = "https://t2i.soulter.top/text2img"
_IMAGE_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _valid_image_id(value):
    """Render ids are relative paths (e.g. ``data/rendered_x.png`` from the
    official-style services); accept path segments but block traversal."""
    if not isinstance(value, (str, int)):
        return False
    image_id = str(value)
    if not image_id or len(image_id) > 256 or "\\" in image_id:
        return False
    return all(
        segment not in {"", ".", ".."} and _IMAGE_ID_SEGMENT_RE.fullmatch(segment)
        for segment in image_id.split("/")
    )
_LOG = logging.getLogger("astrbot")


def normalize_endpoint(value):
    """Accept a text2img base URL or a host root; reject unsafe URL shapes."""
    if not isinstance(value, str) or not value.strip():
        return ""
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(ch.isspace() for ch in url)):
        return ""
    if url.endswith("/generate"):
        url = url[:-len("/generate")]
    if not url.endswith("/text2img"):
        url += "/text2img"
    return url


def _tls_connector():
    """Use the same TLS connector as AstrBot when available."""
    try:
        from astrbot.core.utils.http_ssl import build_tls_connector
        return build_tls_connector()
    except ImportError:
        return None


class T2IRenderClient:
    def __init__(self, custom_endpoints=(), global_endpoint="", endpoint_timeout=10,
                 cooldown=300, logger=None):
        self.logger = logger or _LOG
        self.endpoint_timeout = max(1.0, float(endpoint_timeout))
        self.cooldown = max(0.0, float(cooldown))
        self._preferred = []
        for raw in [*custom_endpoints, global_endpoint]:
            endpoint = normalize_endpoint(raw)
            if endpoint and endpoint not in self._preferred:
                self._preferred.append(endpoint)
        self._official = [DEFAULT_ENDPOINT]
        self._failed_until = {}
        self._last_success = None
        self._lock = asyncio.Lock()

    async def refresh_official_endpoints(self):
        try:
            timeout = aiohttp.ClientTimeout(total=6, connect=3)
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True, connector=_tls_connector()
            ) as session:
                async with session.get(OFFICIAL_POOL_URL) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            raw = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw, list):
                raise ValueError("invalid pool response")
            endpoints = []
            for item in raw:
                if not isinstance(item, dict) or not item.get("active"):
                    continue
                normalized = normalize_endpoint(item.get("url"))
                if normalized and normalized.startswith("https://") and normalized not in endpoints:
                    endpoints.append(normalized)
            if not endpoints:
                raise ValueError("no active HTTPS endpoints")
            async with self._lock:
                self._official = endpoints
            self.logger.info("[MOTD] 官方渲染端点池刷新成功: count=%d", len(endpoints))
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            self.logger.warning("[MOTD] 官方渲染端点池刷新失败，沿用缓存: %s", exc)
            return False

    async def refresh_loop(self):
        while True:
            await self.refresh_official_endpoints()
            await asyncio.sleep(1800)

    async def _candidate_endpoints(self):
        async with self._lock:
            endpoints = list(dict.fromkeys([*self._preferred, *self._official]))
            now = time.monotonic()
            available = [url for url in endpoints if self._failed_until.get(url, 0) <= now]
            if not available and endpoints:
                # Probe exactly one when everything is cooling down.
                available = [min(endpoints, key=lambda url: self._failed_until.get(url, 0))]
            if self._last_success in available:
                available.remove(self._last_success)
                available.insert(0, self._last_success)
            return available

    async def _mark_failed(self, endpoint):
        async with self._lock:
            self._failed_until[endpoint] = time.monotonic() + self.cooldown
            if self._last_success == endpoint:
                self._last_success = None

    async def _mark_succeeded(self, endpoint):
        async with self._lock:
            self._last_success = endpoint
            self._failed_until.pop(endpoint, None)

    async def _try_endpoint(self, endpoint, template, data, timeout):
        payload = {
            "tmpl": template,
            "json": True,
            "tmpldata": data,
            "options": {"full_page": True, "type": "jpeg", "quality": 40},
        }
        client_timeout = aiohttp.ClientTimeout(
            total=timeout, connect=min(3.0, timeout), sock_connect=min(3.0, timeout)
        )
        async with aiohttp.ClientSession(
            timeout=client_timeout, trust_env=True, connector=_tls_connector()
        ) as session:
            async with session.post(f"{endpoint}/generate", json=payload) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                body = await resp.json(content_type=None)
                image_id = body.get("data", {}).get("id") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None
                if not _valid_image_id(image_id):
                    raise ValueError("invalid render id")
                # AstrBot v4.26.7 appends /{id}, not /data/{id}.
                return f"{endpoint}/{image_id}"

    async def render(self, template, data, total_timeout, fallback=None):
        """Never exceed the single render budget, including fallback.

        Keep 20% (up to 4 s) for AstrBot fallback, rather than retrying an
        already exhausted render task for another full timeout.
        """
        deadline = time.monotonic() + max(0.1, float(total_timeout))
        reserve = min(4.0, max(1.0, float(total_timeout) * 0.2)) if fallback else 0.0
        candidates = await self._candidate_endpoints()
        for endpoint in candidates:
            available = deadline - time.monotonic() - reserve
            if available <= 0:
                break
            timeout = min(self.endpoint_timeout, available)
            started = time.monotonic()
            try:
                url = await asyncio.wait_for(
                    self._try_endpoint(endpoint, template, data, timeout), timeout=timeout
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, OSError) as exc:
                await self._mark_failed(endpoint)
                self.logger.warning(
                    "[MOTD] 渲染端点失败: endpoint=%s elapsed=%.2fs reason=%s cooldown=%.0fs",
                    endpoint, time.monotonic() - started, exc, self.cooldown
                )
                continue
            await self._mark_succeeded(endpoint)
            self.logger.info(
                "[MOTD] 渲染端点成功: endpoint=%s elapsed=%.2fs",
                endpoint, time.monotonic() - started
            )
            return url

        remaining = deadline - time.monotonic()
        if fallback is not None and remaining > 0.05:
            self.logger.warning("[MOTD] 所有可试渲染端点失败，启动 AstrBot 内置回退: budget=%.2fs", remaining)
            try:
                url = await asyncio.wait_for(fallback(), timeout=remaining)
                if not isinstance(url, str) or not url:
                    raise ValueError("empty fallback URL")
                return url
            except (asyncio.TimeoutError, ValueError, aiohttp.ClientError, OSError, Exception) as exc:
                self.logger.error("[MOTD] AstrBot 内置渲染回退失败: %s", exc)
        self.logger.error("[MOTD] 渲染失败，耗时受限于总预算 %.1fs", total_timeout)
        return None
