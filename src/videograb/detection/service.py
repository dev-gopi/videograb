"""Fast-first hybrid detector; deep or browser inspection is explicitly requested."""
from __future__ import annotations
import asyncio
import logging
import uuid
from urllib.parse import urlsplit
from .common import item_from_url, media_kind, ident
from .static import parse_html, find_urls
from .manifests import parse_hls, parse_dash
from . import ytdlp, browser
from ..config import Settings
from ..http import SafeHTTP
from ..models import MediaItem, ScanResult
from ..security import UnsafeURL, validate, normalize

log = logging.getLogger(__name__)


class Detector:
    def __init__(self, settings: Settings, http: SafeHTTP):
        self.settings = settings
        self.http = http

    async def _verify_and_recommend(self, items: list[MediaItem]) -> None:
        """Mark formats that respond now, then choose one safe default per item.

        This is deliberately a small, bounded probe: a successful response
        means the URL is currently reachable, not that a protected stream's
        every segment will remain available.
        """
        candidates = [fmt for item in items for fmt in item.formats][:self.settings.max_verify_formats]
        gate = asyncio.Semaphore(4)

        async def probe(fmt):
            headers = {k: v for k, v in fmt.headers.items()
                       if k.lower() in {"referer", "user-agent", "origin", "accept"}}
            try:
                async with gate:
                    response, _ = await asyncio.wait_for(
                        self.http.request(fmt.url, method="HEAD", headers=headers, stream=True),
                        timeout=self.settings.verify_timeout,
                    )
                    try:
                        # Some media origins reject HEAD but accept a tiny
                        # ranged GET.  Do not download or buffer the media.
                        if response.status_code in {405, 501}:
                            await response.aclose()
                            headers["Range"] = "bytes=0-0"
                            response, _ = await asyncio.wait_for(
                                self.http.request(fmt.url, headers=headers, stream=True),
                                timeout=self.settings.verify_timeout,
                            )
                        if 200 <= response.status_code < 400:
                            fmt.accessibility = "response_ok"
                    finally:
                        await response.aclose()
            except Exception:
                # A probe may fail due to a server's anti-bot policy even if
                # yt-dlp can extract it.  Leave it as detected rather than
                # hiding a potentially usable format.
                return

        await asyncio.gather(*(probe(fmt) for fmt in candidates))
        for item in items:
            for fmt in item.formats:
                fmt.recommended = False
            verified_video = [fmt for fmt in item.formats
                              if fmt.accessibility == "response_ok" and fmt.vcodec != "none"]
            if verified_video:
                max(verified_video, key=lambda fmt: (fmt.height or 0, fmt.bitrate or 0,
                                                      fmt.filesize or 0)).recommended = True

    async def scan(self, url: str, *, browser_enabled: bool = False, deep: bool = False) -> ScanResult:
        url = await validate(url, self.settings.allow_private_networks)
        result = ScanResult(scan_id=uuid.uuid4().hex, url=url)
        found: dict[str, MediaItem] = {}
        pending = [(url, 0)]
        seen: set[str] = set()

        def add(item: MediaItem):
            # yt-dlp formats may include the same direct media link as HTML. Prefer its richer metadata.
            if len(found) >= self.settings.max_items:
                return
            key = item.formats[0].url if item.formats and item.extractor != "yt-dlp" else item.id
            if key in found:
                if len(item.formats) > len(found[key].formats):
                    found[key] = item
            else:
                found[key] = item

        # yt-dlp is run once per top-level page; do not run extractor repeatedly on scripts/iframes.
        # HTML first, then yt-dlp; combine rather than discarding multiple embedded videos.
        while pending and len(seen) < self.settings.max_pages:
            page, depth = pending.pop(0)
            extract_page = page
            try:
                page = await validate(page, self.settings.allow_private_networks)
            except UnsafeURL as exc:
                result.warnings.append(f"Blocked embedded URL: {exc}")
                continue
            if page in seen:
                continue
            seen.add(page)
            try:
                document, final_url, mime = await self.http.text(page)
                extract_page = final_url
                if not seen or len(seen) == 1:
                    result.url = final_url
                kind = media_kind(final_url, mime)
                if kind in {"m3u8", "mpd"}:
                    try:
                        item = parse_hls(document, final_url, final_url, "HLS stream") if kind == "m3u8" else parse_dash(document, final_url, final_url, "DASH stream")
                        for fmt in item.formats:
                            fmt.accessibility = "response_ok"  # manifest reached, not its segments
                        add(item)
                    except Exception as exc:
                        result.warnings.append(f"Malformed {kind} manifest: {exc}")
                        x = item_from_url(final_url, page, "Stream", "manifest", mime)
                        if x: add(x)
                elif kind:
                    x = item_from_url(final_url, final_url, "Media file", "direct", mime)
                    if x:
                        x.formats[0].accessibility = "response_ok"
                        add(x)
                elif "html" in mime.lower() or "<!doctype html" in document.lower()[:500] or "<html" in document.lower()[:500]:
                    parsed, iframes, scripts = parse_html(document, final_url)
                    for item in parsed: add(item)
                    if depth < self.settings.max_iframe_depth:
                        pending.extend((x, depth + 1) for x in iframes[:16])
                    for script in scripts[:self.settings.max_scripts]:
                        try:
                            js, js_url, _ = await self.http.text(script)
                            for video in find_urls(js, js_url):
                                x = item_from_url(video, final_url, "Script video", "javascript", allow_unknown=not bool(media_kind(video)))
                                if x: add(x)
                        except Exception:
                            continue
            except Exception as exc:
                result.warnings.append(f"Could not inspect {urlsplit(page).hostname}: {type(exc).__name__}: {str(exc)[:90]}")

            if len(seen) == 1 or (deep and depth > 0):
                for item in await ytdlp.extract(extract_page, timeout=self.settings.request_timeout):
                    add(item)
        result.inspected_pages = len(seen)
        # Expand discovered streaming manifests unless yt-dlp already offers detailed formats.
        manifest_items = [x for x in found.values() if x.extractor not in {"hls", "dash", "yt-dlp"} and x.formats and x.formats[0].protocol in {"m3u8", "mpd"}]
        for old in manifest_items[:12]:
            try:
                fmt = old.formats[0]
                text, resolved, _ = await self.http.text(fmt.url)
                item = parse_hls(text, resolved, old.source_url, old.title) if fmt.protocol == "m3u8" else parse_dash(text, resolved, old.source_url, old.title)
                for stream in item.formats:
                    stream.accessibility = "response_ok"
                found[fmt.url] = item
            except Exception as exc:
                result.warnings.append(f"Manifest detail unavailable: {type(exc).__name__}")
        # Browser stage is opt-in even if static results are empty; installing Chromium is optional.
        if browser_enabled:
            if not self.settings.browser_enabled:
                result.warnings.append("Browser inspection disabled by administrator")
            else:
                browser_items, warnings = await browser.inspect(url, self.settings)
                result.warnings.extend(warnings)
                for item in browser_items: add(item)
        # No attempt is made to defeat DRM, expired signatures, logins or protected streams.
        if not found:
            result.warnings.append("No accessible media links found. It may need playback/browser inspection, credentials, or an unsupported extractor.")
        result.items = list(found.values())[:self.settings.max_items]
        await self._verify_and_recommend(result.items)
        return result
