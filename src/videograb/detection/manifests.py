"""Discover HLS variants, audio and subtitles; summarize DASH representations.

yt-dlp remains the preferred downloading engine for complex segmented streams.
"""
from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from ..models import MediaItem, MediaFormat, Subtitle
from .common import ident


def attrs(line: str) -> dict[str, str]:
    return {m.group(1).upper(): m.group(2).strip('"') for m in re.finditer(r'([\w-]+)=("[^"]*"|[^,]*)', line)}


def parse_hls(text: str, url: str, source: str, title: str) -> MediaItem:
    out = MediaItem(id=ident(url), source_url=source, title=title, extractor="hls")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    current = None
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:"):
            meta = attrs(line)
            if meta.get("TYPE") == "SUBTITLES" and meta.get("URI"):
                out.subtitles.append(Subtitle(language=meta.get("LANGUAGE", "und"), url=urljoin(url, meta["URI"])))
            elif meta.get("TYPE") == "AUDIO" and meta.get("URI"):
                v = urljoin(url, meta["URI"])
                out.formats.append(MediaFormat(id=ident(v, "audio"), url=v, protocol="m3u8", ext="m4a", backend="ffmpeg", acodec="unknown", vcodec="none", language=meta.get("LANGUAGE"), quality="audio", headers={"Referer": source} if source != url else {}))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            current = attrs(line)
        elif not line.startswith("#") and current is not None:
            variant = urljoin(url, line)
            res = current.get("RESOLUTION", "").split("x")
            w, h = (int(res[0]), int(res[1])) if len(res) == 2 and all(x.isdigit() for x in res) else (None, None)
            bandwidth = current.get("AVERAGE-BANDWIDTH") or current.get("BANDWIDTH")
            out.formats.append(MediaFormat(id=ident(variant), url=variant, backend="ffmpeg", protocol="m3u8", quality=f"{h}p" if h else None, width=w, height=h, bitrate=int(bandwidth) / 1000 if bandwidth and bandwidth.isdigit() else None,
                                            headers={"Referer": source} if source != url else {}))
            current = None
    if not any(f.vcodec != "none" for f in out.formats):
        out.formats.append(MediaFormat(id=ident(url, "master"), url=url, ext="mp4", backend="ffmpeg", protocol="m3u8", quality="auto", headers={"Referer": source} if source != url else {}))
    # HLS variants can contain audio group associations; pass top-level manifest to yt-dlp for robust selection.
    return out


def parse_dash(text: str, url: str, source: str, title: str) -> MediaItem:
    root = ET.fromstring(text)
    item = MediaItem(id=ident(url), source_url=source, title=title, extractor="dash")
    resolutions: list[str] = []
    for rep in root.iter():
        if rep.tag.rsplit("}", 1)[-1] != "Representation":
            continue
        height = _int(rep.attrib.get("height"))
        bw = _int(rep.attrib.get("bandwidth"))
        if height:
            resolutions.append(f"{height}p")
        elif bw:
            resolutions.append(f"{round(bw/1000)}kbps")
    note = "auto" + (" (tracks: " + ", ".join(list(dict.fromkeys(resolutions))[:10]) + ")" if resolutions else "")
    # Do not promise a representation selector when FFmpeg is only given the MPD.
    # A yt-dlp extractor may expose genuinely selectable DASH formats separately.
    item.formats.append(MediaFormat(id=ident(url, "auto"), url=url, ext="mp4", protocol="mpd", backend="ffmpeg", quality=note, headers={"Referer": source} if source != url else {}))
    return item


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
