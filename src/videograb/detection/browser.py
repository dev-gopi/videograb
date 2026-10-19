"""Opt-in Chromium stage: DOM across accessible frames and network responses.

This is not a fully isolated browser. Browser stage is opt-in and local-only.
"""
from __future__ import annotations
import asyncio
import logging
from .common import item_from_url, media_kind
from .static import find_urls
from ..models import MediaItem
from ..security import validate
from ..config import Settings

log = logging.getLogger(__name__)


async def inspect(url: str, settings: Settings) -> tuple[list[MediaItem], list[str]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], ["Playwright is not installed: pip install 'videograb[browser]' then playwright install chromium"]
    items: dict[str, MediaItem] = {}
    warnings: list[str] = []
    response_tasks: set[asyncio.Task] = set()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-first-run"])
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block", ignore_https_errors=False)
                requests = 0
                async def guard(route):
                    nonlocal requests
                    requests += 1
                    if requests > 350:
                        await route.abort()
                        return
                    try:
                        await validate(route.request.url, settings.allow_private_networks)
                        await route.continue_()
                    except Exception:
                        await route.abort()

                await context.route("**/*", guard)
                async def parse_text_response(response):
                    try:
                        body = await asyncio.wait_for(response.body(), timeout=2.0)
                        if len(body) > 256 * 1024:
                            return
                        for candidate in find_urls(body.decode("utf-8", errors="replace"), response.url):
                            x = item_from_url(candidate, url, "Player API media", "browser-api", allow_unknown=not bool(media_kind(candidate)))
                            if x:
                                ref = response.request.headers.get("referer")
                                if ref and ref.startswith(("http://", "https://")):
                                    x.formats[0].headers["Referer"] = ref
                                items[x.formats[0].url] = x
                    except Exception:
                        return

                def response_seen(response):
                    mime = response.headers.get("content-type", "")
                    kind = media_kind(response.url, mime)
                    if kind and not response.url.lower().split("?", 1)[0].endswith((".m4s", ".vtt", ".aac", ".m4a")):
                        x = item_from_url(response.url, url, "Browser discovered media", "browser-network", mime)
                        if x:
                            if 200 <= response.status < 400:
                                x.formats[0].accessibility = "response_ok"
                            ref = response.request.headers.get("referer")
                            if ref and ref.startswith(("http://", "https://")):
                                x.formats[0].headers["Referer"] = ref
                            items[x.formats[0].url] = x
                    elif len(response_tasks) < 35 and any(m in mime.lower() for m in ("application/json", "text/javascript", "application/javascript")) and response.request.resource_type in {"xhr", "fetch"}:
                        length = response.headers.get("content-length", "")
                        if length and (not length.isdigit() or int(length) > 256 * 1024):
                            return
                        task = asyncio.create_task(parse_text_response(response))
                        response_tasks.add(task)
                        task.add_done_callback(response_tasks.discard)
                context.on("response", response_seen)
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=settings.browser_timeout_ms)
                for _ in range(settings.browser_scrolls):
                    await page.evaluate("window.scrollBy(0, Math.max(innerHeight, 500))")
                    await page.wait_for_timeout(400)
                # Only trigger HTML video playback; never click external buttons/login/paywalls.
                await page.evaluate("""() => { document.querySelectorAll('video').forEach(v => {v.preload='auto';v.play().catch(()=>{});}); }""")
                await page.wait_for_timeout(settings.browser_settle_ms)
                if response_tasks:
                    done, pending = await asyncio.wait(response_tasks, timeout=3)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                for frame in page.frames[:24]:
                    try:
                        discovered = await frame.evaluate("""() => Array.from(document.querySelectorAll('video,source,[data-video],[data-src]')).flatMap(v => [v.currentSrc, v.src, v.getAttribute('src'), v.getAttribute('data-src'), v.getAttribute('data-video')]).filter(Boolean)""")
                        for media in discovered:
                            x = item_from_url(str(media), frame.url or url, "Rendered video", "browser-dom")
                            if x:
                                items[x.formats[0].url] = x
                    except Exception:
                        continue
                await context.close()
            finally:
                pending = tuple(response_tasks)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await browser.close()
    except Exception as exc:
        warnings.append(f"Browser inspection unavailable: {str(exc)[:180]}")
    return list(items.values())[:settings.max_items], warnings
