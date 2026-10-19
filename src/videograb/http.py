"""Bounded HTTP fetches with per-hop redirect and DNS validation."""
from __future__ import annotations
import httpx
from .config import Settings
from .security import validate, UnsafeURL


class SafeHTTP:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout), follow_redirects=False, trust_env=False,
                                        headers={"User-Agent": "VideoGrab/0.1 (+local video detector)", "Accept": "*/*"})

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                      stream: bool = False) -> tuple[httpx.Response, str]:
        """Caller must close a streaming response (response.aclose)."""
        current = url
        for _ in range(self.settings.max_redirects + 1):
            current = await validate(current, self.settings.allow_private_networks)
            req = self.client.build_request(method, current, headers=headers)
            response = await self.client.send(req, stream=True)
            if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                next_url = httpx.URL(current).join(response.headers["location"])
                await response.aclose()
                current = str(next_url)
                continue
            if not stream:
                await response.aread()
                await response.aclose()
            return response, current
        raise UnsafeURL("Too many redirects")

    async def text(self, url: str, *, headers: dict[str, str] | None = None) -> tuple[str, str, str]:
        response, final_url = await self.request(url, headers=headers, stream=True)
        try:
            response.raise_for_status()
            # Never buffer large progressive video files just to discover their MIME.
            from .detection.common import media_kind
            if media_kind(final_url, response.headers.get("content-type", "")) in {"mp4", "webm", "mov", "mkv", "m4v", "avi", "ts", "ogv", "mpeg", "mpg"}:
                return "", final_url, response.headers.get("content-type", "")
            buf = bytearray()
            async for block in response.aiter_bytes():
                buf.extend(block)
                if len(buf) > self.settings.max_scan_bytes:
                    raise ValueError("Page or manifest exceeds scan size limit")
            import codecs
            charset = response.encoding or "utf-8"
            try:
                decoded = buf.decode(charset, errors="replace")
            except (LookupError, UnicodeError):
                decoded = buf.decode("utf-8", errors="replace")
            return decoded, final_url, response.headers.get("content-type", "")
        finally:
            await response.aclose()
