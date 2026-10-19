from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class MediaFormat(BaseModel):
    id: str
    url: str
    ext: str = "mp4"
    protocol: str = "http"
    quality: str | None = None
    width: int | None = None
    height: int | None = None
    bitrate: float | None = None
    filesize: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    language: str | None = None
    backend: Literal["direct", "ffmpeg", "ytdlp"] = "direct"
    accessibility: Literal["detected", "response_ok"] = "detected"
    recommended: bool = False
    selector: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class Subtitle(BaseModel):
    language: str
    url: str
    ext: str = "vtt"


class MediaItem(BaseModel):
    id: str
    title: str
    source_url: str
    extractor: str
    thumbnail: str | None = None
    duration: float | None = None
    formats: list[MediaFormat] = Field(default_factory=list)
    subtitles: list[Subtitle] = Field(default_factory=list)


class ScanResult(BaseModel):
    scan_id: str
    url: str
    items: list[MediaItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    inspected_pages: int = 0


class ScanRequest(BaseModel):
    url: str
    browser: bool = False
    deep: bool = False


class DownloadRequest(BaseModel):
    scan_id: str
    item_id: str
    format_id: str | None = None
    audio_only: bool = False
    subtitles: bool = False


class BulkDownloadRequest(BaseModel):
    selections: list[DownloadRequest] = Field(min_length=1, max_length=60)


class DownloadJob(BaseModel):
    id: str
    status: Literal["queued", "downloading", "paused", "completed", "failed", "cancelled"] = "queued"
    title: str
    source_url: str
    item: MediaItem
    format_id: str | None = None
    audio_only: bool = False
    subtitles: bool = False
    received: int = 0
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    output_path: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
