"""Persistent asyncio download queue, resumable HTTP transfer and FFmpeg/yt-dlp engines."""
from __future__ import annotations
import asyncio
import contextlib
import httpx
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal
from ..config import Settings
from ..http import SafeHTTP
from ..models import DownloadJob, DownloadRequest, MediaFormat
from ..security import safe_name, validate
from ..storage import Storage, now

log = logging.getLogger(__name__)


class HaltDownload(Exception):
    def __init__(self, state: str):
        self.state = state


class DownloadManager:
    def __init__(self, settings: Settings, storage: Storage, http: SafeHTTP):
        self.settings = settings
        self.storage = storage
        self.http = http
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.halts: dict[str, str] = {}
        self.active: set[str] = set()
        self.resume_pending: set[str] = set()
        self._last_update: dict[str, float] = {}

    async def start(self) -> None:
        if self.workers:
            return
        for job in reversed(self.storage.list_jobs(500)):
            if job.status in {"queued", "downloading"}:
                job.status = "queued"
                job.updated_at = now()
                self.storage.save_job(job)
                self.queue.put_nowait(job.id)
        for _ in range(self.settings.download_workers):
            self.workers.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for task in self.workers:
            task.cancel()
        for task in self.workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.workers.clear()

    async def enqueue(self, selection: DownloadRequest) -> DownloadJob:
        scan = self.storage.get_scan(selection.scan_id)
        if not scan:
            raise ValueError("Scan expired or not found. Scan the URL again.")
        item = next((x for x in scan.items if x.id == selection.item_id), None)
        if not item:
            raise ValueError("Video not present in scan")
        fmt = next((x for x in item.formats if x.id == selection.format_id), None) if selection.format_id else next(iter(item.formats), None)
        if not fmt:
            raise ValueError("Selected format not available")
        await validate(item.source_url, self.settings.allow_private_networks)
        await validate(fmt.url, self.settings.allow_private_networks)
        # Avoid duplicate queued/running/completed downloads for the same selection.
        for previous in self.storage.list_jobs(200):
            if previous.source_url == item.source_url and previous.format_id == fmt.id and previous.audio_only == selection.audio_only and previous.subtitles == selection.subtitles:
                if previous.status in {"queued", "downloading", "paused"}:
                    return previous
                if previous.status == "completed" and previous.output_path and Path(previous.output_path).is_file():
                    return previous
        # Never persist authorization or session cookies; only benign origin/referrer headers.
        job = DownloadJob(id=uuid.uuid4().hex[:14], title=item.title, source_url=item.source_url,
                          item=item, format_id=fmt.id, audio_only=selection.audio_only,
                          subtitles=selection.subtitles, created_at=now(), updated_at=now())
        self.storage.save_job(job)
        await self.queue.put(job.id)
        return job

    async def control(self, job_id: str, action: Literal["pause", "resume", "cancel"]) -> DownloadJob:
        job = self.storage.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if action == "resume":
            if job.status not in {"paused", "failed", "cancelled"}:
                raise ValueError("Only paused, failed or cancelled jobs can resume")
            job.error = None
            job.updated_at = now()
            if job_id in self.active:
                # A paused download may still be closing its network/FFmpeg handle.
                # Schedule resume only after the original worker has released it.
                self.resume_pending.add(job_id)
                self.storage.save_job(job)
            else:
                self.halts.pop(job_id, None)
                job.status = "queued"
                self.storage.save_job(job)
                await self.queue.put(job_id)
        else:
            if job.status in {"completed", "failed", "cancelled"}:
                raise ValueError("Job can no longer be paused or cancelled")
            new_status = "paused" if action == "pause" else "cancelled"
            self.halts[job_id] = new_status
            job.status = new_status
            job.updated_at = now()
            self.storage.save_job(job)
        return job

    def _check(self, job: DownloadJob) -> None:
        state = self.halts.get(job.id)
        if state:
            raise HaltDownload(state)

    def _progress(self, job: DownloadJob, received: int, total: int | None = None,
                  speed: float | None = None, eta: float | None = None) -> None:
        self._check(job)
        if received > self.settings.max_download_bytes or (total is not None and total > self.settings.max_download_bytes):
            raise ValueError("Download exceeds configured size limit")
        job.received = max(0, int(received))
        job.total = total
        job.speed = speed
        job.eta = eta
        clock = time.monotonic()
        if clock - self._last_update.get(job.id, 0) > .35:
            job.updated_at = now()
            self.storage.save_job(job)
            self._last_update[job.id] = clock

    def _fmt(self, job: DownloadJob) -> MediaFormat:
        return next(f for f in job.item.formats if f.id == job.format_id)

    def _base(self, job: DownloadJob) -> Path:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        # Created time stays stable across pause/resume; the job ID prevents
        # collisions even when identical titles are queued in the same second.
        timestamp = re.sub(r"[^0-9]", "", job.created_at)[:14] or "download"
        return self.settings.output_dir / f"{safe_name(job.title)}-{timestamp}-{job.id}"

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                job = self.storage.get_job(job_id)
                if job and job.status == "queued" and job_id not in self.active:
                    self.active.add(job_id)
                    try:
                        await self.run(job)
                    finally:
                        self.active.discard(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unhandled worker error")
            finally:
                self.queue.task_done()

    async def run(self, job: DownloadJob) -> DownloadJob:
        job.status = "downloading"
        job.updated_at = now()
        self.storage.save_job(job)
        try:
            fmt = self._fmt(job)
            await validate(fmt.url, self.settings.allow_private_networks)
            if fmt.backend == "ytdlp":
                path = await self._ytdlp(job, fmt)
            elif fmt.backend == "ffmpeg" or job.audio_only:
                path = await self._ffmpeg(job, fmt)
            else:
                path = await self._http(job, fmt)
            self._check(job)
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("Download completed without a valid output file")
            if job.subtitles and fmt.backend != "ytdlp" and job.item.subtitles:
                try:
                    await self._subtitles(job)
                except Exception as exc:
                    job.error = f"Video saved, but subtitles failed: {exc}"
            if path.stat().st_size > self.settings.max_download_bytes:
                path.unlink(missing_ok=True)
                raise ValueError("Final file exceeds size limit")
            job.status = "completed"
            job.output_path = str(path)
            job.received = path.stat().st_size
            job.total = path.stat().st_size
            job.speed = None
            job.eta = 0
        except HaltDownload as exc:
            job.status = exc.state
        except asyncio.CancelledError:
            job.status = "queued"  # resume after application restart
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {str(exc)[:350]}"
            log.warning("Download failed: %s", job.error)
        finally:
            job.updated_at = now()
            self.storage.save_job(job)
            self.halts.pop(job.id, None)
            self._last_update.pop(job.id, None)
            if job.id in self.resume_pending and job.status in {"paused", "cancelled"}:
                self.resume_pending.discard(job.id)
                job.status = "queued"
                job.updated_at = now()
                self.storage.save_job(job)
                self.queue.put_nowait(job.id)
        return job

    async def _subtitles(self, job: DownloadJob) -> None:
        for sub in job.item.subtitles[:12]:
            await validate(sub.url, self.settings.allow_private_networks)
            ext = sub.ext.lower() if sub.ext.lower() in {"vtt", "srt", "ttml", "ass"} else "vtt"
            language = safe_name(sub.language, 14)
            target = self._base(job).with_name(self._base(job).name + f".{language}.{ext}")
            response, _ = await self.http.request(sub.url, stream=True)
            try:
                response.raise_for_status()
                count = 0
                with target.open("wb") as f:
                    async for block in response.aiter_bytes(chunk_size=65536):
                        count += len(block)
                        if count > 5 * 1024 * 1024:
                            raise ValueError("Subtitle too large")
                        f.write(block)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            finally:
                await response.aclose()

    async def _http(self, job: DownloadJob, fmt: MediaFormat) -> Path:
        target = Path(str(self._base(job)) + f".{fmt.ext if fmt.ext in {'mp4','webm','mov','mkv','m4v','avi','ts','ogv','mpg','mpeg'} else 'mp4'}")
        part = Path(str(target) + ".part")
        existing = part.stat().st_size if part.exists() else 0
        headers = {k: v for k, v in fmt.headers.items() if k.lower() in {"user-agent", "referer", "origin", "accept"}}
        headers["Accept-Encoding"] = "identity"
        for attempt in range(3):
            self._check(job)
            if existing:
                headers["Range"] = f"bytes={existing}-"
            else:
                headers.pop("Range", None)
            try:
                response, _ = await self.http.request(fmt.url, stream=True, headers=headers)
                try:
                    if response.status_code in {408, 429, 500, 502, 503, 504}:
                        response.raise_for_status()
                    if response.status_code == 416 and existing:
                        content_range = response.headers.get("Content-Range", "")
                        if content_range == f"bytes */{existing}":
                            os.replace(part, target)
                            return target
                    response.raise_for_status()
                    if existing and response.status_code != 206:
                        existing = 0
                    if existing and not response.headers.get("Content-Range", "").startswith(f"bytes {existing}-"):
                        existing = 0
                    length = int(response.headers.get("Content-Length", 0)) or None
                    # A 206 response can communicate the complete object
                    # size even when Content-Length only describes the
                    # remaining range.  Prefer it for an accurate progress
                    # bar after resume.
                    content_range = response.headers.get("Content-Range", "")
                    range_total = None
                    if "/" in content_range:
                        try:
                            range_total = int(content_range.rsplit("/", 1)[1])
                        except ValueError:
                            pass
                    total = range_total or ((existing + length) if length is not None else None)
                    if total is not None and total > self.settings.max_download_bytes:
                        raise ValueError("Content-Length exceeds maximum")
                    remaining = (total - existing) if total is not None else 16 * 1024 * 1024
                    if shutil.disk_usage(self.settings.output_dir).free < remaining + 8 * 1024 * 1024:
                        raise OSError("Not enough disk space")
                    start = time.monotonic()
                    received = existing
                    with part.open("ab" if existing else "wb") as output:
                        async for block in response.aiter_bytes(chunk_size=256 * 1024):
                            self._check(job)
                            output.write(block)
                            received += len(block)
                            elapsed = max(time.monotonic() - start, .001)
                            speed = (received - existing) / elapsed
                            self._progress(job, received, total, speed, (total - received) / speed if total and speed else None)
                            if received % (8 * 1024 * 1024) < len(block) and shutil.disk_usage(self.settings.output_dir).free < 8 * 1024 * 1024:
                                raise OSError("Disk space critically low")
                    if total is not None and received != total:
                        raise IOError("Incomplete HTTP transfer")
                    os.replace(part, target)
                    return target
                finally:
                    await response.aclose()
            except (OSError, TimeoutError, httpx.TransportError, httpx.HTTPStatusError) as exc:
                existing = part.stat().st_size if part.exists() else 0
                if attempt == 2 or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {408, 429, 500, 502, 503, 504}):
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        raise IOError("HTTP transfer failed")

    async def _ffmpeg(self, job: DownloadJob, fmt: MediaFormat) -> Path:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is required for HLS, DASH and audio conversion")
        ext = "mp3" if job.audio_only else "mp4"
        target = Path(str(self._base(job)) + "." + ext)
        part = self._base(job).with_name(self._base(job).name + ".temp." + ext)
        # ffmpeg must never receive credentials or host-internal URLs. See security notes.
        headers = "".join(f"{k}: {v}\r\n" for k, v in fmt.headers.items() if k.lower() in {"referer", "user-agent", "origin", "accept"} and "\r" not in v and "\n" not in v)
        args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if headers:
            args += ["-headers", headers]
        args += ["-i", fmt.url]
        if job.audio_only:
            args += ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
        else:
            args += ["-c", "copy", "-movflags", "+faststart"]
        args += ["-progress", "pipe:1", "-f", ext, str(part)]
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stderr_chunks: list[bytes] = []
        async def read_errors():
            while line := await process.stderr.readline():
                stderr_chunks.append(line[:1000])
                if len(stderr_chunks) > 30:
                    stderr_chunks.pop(0)
        error_task = asyncio.create_task(read_errors())
        try:
            while True:
                self._check(job)
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=.4)
                except asyncio.TimeoutError:
                    line = b""
                if part.exists():
                    self._progress(job, part.stat().st_size)
                    if shutil.disk_usage(self.settings.output_dir).free < 8 * 1024 * 1024:
                        raise OSError("Disk space critically low")
                if not line and process.returncode is not None:
                    break
                if not line:
                    await asyncio.sleep(.1)
                if process.stdout.at_eof():
                    break
            rc = await process.wait()
            if rc != 0:
                raise RuntimeError((b"".join(stderr_chunks).decode(errors="replace")[-450:]) or f"FFmpeg exited {rc}")
            os.replace(part, target)
            return target
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            await error_task

    async def _ytdlp(self, job: DownloadJob, fmt: MediaFormat) -> Path:
        from yt_dlp import YoutubeDL
        base = self._base(job)
        # yt-dlp's native resume/fragment support is retained through .part files.
        loop = asyncio.get_running_loop()
        def blocking() -> Path:
            selector = fmt.selector or "best"
            if job.audio_only:
                selector = "bestaudio/best"
            elif fmt.vcodec and fmt.vcodec != "none" and fmt.acodec == "none":
                selector = f"{selector}+bestaudio/{selector}"
            def hook(state: dict):
                self._check(job)
                if state.get("status") == "downloading":
                    self._progress(job, state.get("downloaded_bytes") or 0,
                                   state.get("total_bytes") or state.get("total_bytes_estimate"),
                                   state.get("speed"), state.get("eta"))
            opts = {"quiet": True, "no_warnings": True, "format": selector,
                    "outtmpl": str(base) + ".%(ext)s", "merge_output_format": "mp4",
                    "continuedl": True, "retries": 3, "fragment_retries": 3,
                    "socket_timeout": self.settings.request_timeout, "cachedir": False,
                    "noplaylist": True, "progress_hooks": [hook], "overwrites": False,
                    "max_filesize": self.settings.max_download_bytes,
                    "restrictfilenames": False}
            # Preserve non-sensitive request context discovered by the
            # extractor.  Cookies and authorization credentials are never
            # stored by this application.
            request_headers = {k: v for k, v in fmt.headers.items()
                               if k.lower() in {"referer", "user-agent", "origin", "accept"}
                               and "\r" not in v and "\n" not in v}
            if request_headers:
                opts["http_headers"] = request_headers
            if job.audio_only:
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            if job.subtitles:
                opts.update({"writesubtitles": True, "subtitleslangs": ["all"]})
            with YoutubeDL(opts) as ydl:
                # Re-extract at download time: signed streaming URLs expire.
                ydl.download([job.source_url])
            matches = [p for p in base.parent.glob(base.name + ".*") if p.is_file() and not p.name.endswith((".part", ".ytdl", ".vtt", ".srt", ".temp.mp4", ".temp.mp3", ".json"))]
            if not matches:
                raise IOError("yt-dlp did not produce a media file")
            return max(matches, key=lambda x: x.stat().st_size)
        return await asyncio.to_thread(blocking)
