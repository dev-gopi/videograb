from __future__ import annotations
from hashlib import sha256
from urllib.parse import urlsplit
from ..models import MediaFormat, MediaItem
from ..security import normalize

MEDIA_EXTS = {"mp4", "webm", "mov", "mkv", "m4v", "avi", "ts", "mpg", "mpeg", "ogv", "m3u8", "mpd"}
VIDEO_MIMES = ("video/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml")


def media_kind(url: str, mime: str = "") -> str | None:
    ext = urlsplit(url).path.rsplit(".", 1)[-1].lower() if "." in urlsplit(url).path else ""
    if ext in MEDIA_EXTS:
        return ext
    mime = mime.lower().split(";")[0]
    if "mpegurl" in mime:
        return "m3u8"
    if "dash+xml" in mime:
        return "mpd"
    if mime.startswith("video/"):
        return {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}.get(mime, "mp4")
    return None


def ident(*values: str) -> str:
    return sha256("\x00".join(values).encode()).hexdigest()[:16]


def item_from_url(url: str, source: str, title: str, extractor: str, mime: str = "", *, allow_unknown: bool = False) -> MediaItem | None:
    try:
        url = normalize(url, source)
    except ValueError:
        return None
    kind = media_kind(url, mime)
    if not kind and not allow_unknown:
        return None
    # Player APIs commonly return signed, extensionless endpoints.  A URL
    # obtained from an explicit media field is still useful to offer to the
    # user, even before its response MIME type can be observed.
    kind = kind or "mp4"
    backend = "ffmpeg" if kind in {"m3u8", "mpd"} else "direct"
    return MediaItem(id=ident(url), title=title, source_url=source, extractor=extractor,
                     formats=[MediaFormat(id=ident(url, "format"), url=url, ext="mp4" if backend == "ffmpeg" else kind,
                                          protocol=kind if backend == "ffmpeg" else "http", backend=backend,
                                          headers={"Referer": source} if source != url and source.startswith(("http://", "https://")) else {})])
