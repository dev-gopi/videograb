"""Embed yt-dlp using its public YoutubeDL interface; never parse console output."""
from __future__ import annotations
import asyncio
import logging
from collections.abc import Mapping
from itertools import islice
from urllib.parse import urlsplit
from ..models import MediaFormat, MediaItem, Subtitle
from .common import ident

log = logging.getLogger(__name__)


def info_to_items(info: dict, source: str, limit: int = 25) -> list[MediaItem]:
    if info.get("_type") in {"playlist", "multi_video"}:
        output: list[MediaItem] = []
        for entry in islice((info.get("entries") or []), limit):
            if isinstance(entry, dict):
                output.extend(info_to_items(entry, source, limit))
        return output[:limit]
    if not info.get("url") and not info.get("formats"):
        return []
    uid = str(info.get("id") or ident(str(info.get("webpage_url", source))))
    title = str(info.get("title") or "Video")
    item = MediaItem(id=ident("ytdlp", source, uid), title=title[:200], source_url=str(info.get("webpage_url") or source),
                     extractor="yt-dlp", thumbnail=info.get("thumbnail"), duration=info.get("duration"))
    for f in (info.get("formats") or [info]):
        if not isinstance(f, Mapping) or not f.get("url"):
            continue
        fmt_url = str(f["url"])
        if urlsplit(fmt_url).scheme not in {"http", "https"}:
            continue
        fmt_id = str(f.get("format_id") or "best")
        headers = {k: v for k, v in (f.get("http_headers") or info.get("http_headers") or {}).items()
                   if k.lower() in {"referer", "user-agent", "origin", "accept"} and isinstance(v, str)}
        item.formats.append(MediaFormat(id=ident(item.id, fmt_id, fmt_url), url=fmt_url,
                                        ext=str(f.get("ext") or "mp4"), protocol=str(f.get("protocol") or "http"),
                                        quality=str(f.get("format_note") or (f"{f['height']}p" if f.get("height") else fmt_id)),
                                        width=f.get("width"), height=f.get("height"), bitrate=f.get("tbr"),
                                        filesize=f.get("filesize") or f.get("filesize_approx"),
                                        vcodec=f.get("vcodec"), acodec=f.get("acodec"), language=f.get("language"),
                                        backend="ytdlp", selector=fmt_id, headers=headers))
    for language, tracks in (info.get("subtitles") or {}).items():
        for track in tracks or []:
            if isinstance(track, dict) and isinstance(track.get("url"), str) and urlsplit(track["url"]).scheme in {"http", "https"}:
                item.subtitles.append(Subtitle(language=language, url=track["url"], ext=track.get("ext") or "vtt"))
    return [item] if item.formats else []


async def extract(url: str, timeout: float = 15.0) -> list[MediaItem]:
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return []

    def blocking() -> list[MediaItem]:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "ignoreerrors": True, "noplaylist": False, "playlistend": 20,
                "socket_timeout": timeout, "cachedir": False, "extract_flat": False,
                "nocheckcertificate": False, "retries": 1, "extractor_retries": 1,
                "http_headers": {"User-Agent": "Mozilla/5.0 (VideoGrab local downloader)"}}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info_to_items(info, url) if isinstance(info, dict) else []

    try:
        return await asyncio.wait_for(asyncio.to_thread(blocking), timeout=timeout * 3)
    except Exception as exc:
        log.info("yt-dlp extraction unavailable for %s: %s", urlsplit(url).hostname, type(exc).__name__)
        return []
