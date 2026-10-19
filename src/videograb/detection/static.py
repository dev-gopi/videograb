"""Static DOM, player-object, JSON-LD and external JavaScript URL discovery."""
from __future__ import annotations
import html
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .common import item_from_url, media_kind
from ..models import MediaItem, Subtitle

URL_RE = re.compile(r'''(?:(?:https?:)?//|\\?/|\.\.?/)[^\s<>"'`\\]+?\.(?:m3u8|mpd|mp4|webm|m4v|mov|mkv|avi|ts)(?:\?[^\s<>"'`\\]*)?''', re.I)
# These are media-specific player configuration keys.  Unlike a generic URL
# regex, accepting an extensionless value here catches signed API/CDN links
# without treating every link on a page as a video.
JS_FIELD_RE = re.compile(r'''["']?(?:file|src|source|videoUrl|video_url|playUrl|hls|dash|contentUrl|manifest|playlist|streamUrl|stream_url|playbackUrl|playback_url)["']?\s*:\s*["']([^"']+)["']''', re.I)
JS_ARRAY_RE = re.compile(r'''["']?(?:sources|files|streams|playlist)["']?\s*:\s*\[\s*["']([^"']+)["']''', re.I)
MEDIA_FIELDS = {"file", "src", "source", "videourl", "playurl", "hls", "dash", "contenturl", "manifest", "playlist", "streamurl", "playbackurl", "mediaurl", "videofile"}


def unescape(value: str) -> str:
    value = html.unescape(value.strip())
    try:
        value = value.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
        from urllib.parse import unquote
        if "%2F" in value.upper() or "%3A" in value.upper():
            value = unquote(value)
    except ValueError:
        pass
    return value


def find_urls(data: str, base: str) -> list[str]:
    contextual = list(JS_FIELD_RE.findall(data))
    contextual += list(JS_ARRAY_RE.findall(data))
    raw = contextual + list(URL_RE.findall(data))
    # JSON string unicode-escaped URLs are common in player configs.
    raw += re.findall(r'https?\\/\\/[^\s"\']+', data)
    found: dict[str, None] = {}
    for index, s in enumerate(raw):
        s = unescape(s).rstrip(",;)]}")
        s = urljoin(base, s)
        if media_kind(s) or index < len(contextual):
            found[s] = None
    return list(found)


def config_urls(value: object, base: str, limit: int = 80) -> list[str]:
    """Find explicit media fields in JSON player configuration objects."""
    found: dict[str, None] = {}

    def visit(node: object, contextual: bool = False) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, str):
            if contextual:
                candidate = urljoin(base, unescape(node))
                if candidate.startswith(("http://", "https://")):
                    found[candidate] = None
        elif isinstance(node, dict):
            for key, child in node.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                visit(child, contextual or normalized in MEDIA_FIELDS)
        elif isinstance(node, list):
            for child in node:
                visit(child, contextual)

    visit(value)
    return list(found)


def parse_html(document: str, url: str) -> tuple[list[MediaItem], list[str], list[str]]:
    soup = BeautifulSoup(document, "html.parser")
    # Player pages often use a CDN <base> URL.  Resolve assets against it,
    # while preserving the actual page URL as each media item's source.
    base_tag = soup.find("base", href=True)
    asset_base = urljoin(url, str(base_tag["href"])) if base_tag else url
    title = (soup.title.get_text(" ", strip=True) if soup.title else "Video")[:160]
    media_urls: dict[str, bool] = {}
    subtitle_urls: dict[str, Subtitle] = {}
    iframes: list[str] = []
    scripts: list[str] = []
    for tag in soup.find_all(True):
        if tag.name == "iframe" and tag.get("src"):
            iframes.append(urljoin(asset_base, str(tag["src"])))
        if tag.name == "script" and tag.get("src"):
            scripts.append(urljoin(asset_base, str(tag["src"])))
        if tag.name == "track" and str(tag.get("kind", "")).lower() in {"captions", "subtitles"} and tag.get("src"):
            sub_url = urljoin(asset_base, unescape(str(tag["src"])))
            subtitle_urls[sub_url] = Subtitle(language=str(tag.get("srclang") or tag.get("label") or "und")[:32], url=sub_url,
                                              ext="srt" if sub_url.lower().split("?", 1)[0].endswith(".srt") else "vtt")
        for key, value in tag.attrs.items():
            if key in {"src", "data-src", "data-video", "data-url", "href", "content", "poster", "data-hls", "data-mpd", "data-file", "data-source"}:
                if isinstance(value, str):
                    candidate = urljoin(asset_base, unescape(value))
                    known = bool(media_kind(candidate))
                    # data-video/file/source are explicit player fields and
                    # often contain extensionless signed endpoints.
                    contextual = key in {"data-video", "data-hls", "data-mpd", "data-file", "data-source"} or (tag.name in {"video", "source", "audio"} and key == "src")
                    if known or contextual:
                        media_urls[candidate] = not known
            if tag.name == "meta" and key == "content" and isinstance(value, str):
                property_name = str(tag.get("property") or tag.get("name") or "").lower()
                if property_name.startswith(("og:video", "twitter:player:stream")):
                    candidate = urljoin(asset_base, unescape(value))
                    media_urls[candidate] = not bool(media_kind(candidate))
            if key in {"data-setup", "data-config", "data-options", "data-player"} and isinstance(value, str):
                try:
                    for candidate in config_urls(json.loads(html.unescape(value)), asset_base):
                        media_urls[candidate] = not bool(media_kind(candidate))
                except (ValueError, TypeError):
                    pass
        if tag.name == "script" and not tag.get("src"):
            body = tag.string or tag.get_text()
            for candidate in find_urls(body, asset_base):
                media_urls[candidate] = not bool(media_kind(candidate))
            if tag.get("type") == "application/ld+json":
                try:
                    value = json.loads(body)
                    def visit(obj):
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k in {"contentUrl", "embedUrl", "url"} and isinstance(v, str):
                                    candidate = urljoin(asset_base, v)
                                    # Schema.org VideoObject commonly uses an
                                    # extensionless contentUrl.
                                    if media_kind(candidate) or k == "contentUrl":
                                        media_urls[candidate] = not bool(media_kind(candidate))
                                visit(v)
                        elif isinstance(obj, list):
                            for entry in obj:
                                visit(entry)
                    visit(value)
                except (ValueError, RecursionError):
                    pass
            else:
                # Many players embed strict JSON in a regular script tag.
                # Parse it when possible; otherwise retain regex discovery.
                try:
                    for candidate in config_urls(json.loads(body), asset_base):
                        media_urls[candidate] = not bool(media_kind(candidate))
                except (ValueError, TypeError, RecursionError):
                    pass
    items = [x for media, unknown in media_urls.items() if (x := item_from_url(media, url, title, "html", allow_unknown=unknown))]
    for item in items:
        item.subtitles.extend(subtitle_urls.values())
    return items, list(dict.fromkeys(iframes)), list(dict.fromkeys(scripts))
