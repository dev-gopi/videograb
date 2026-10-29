"""Typer CLI over the same detector, download manager, and SQLite history as the UI."""
from __future__ import annotations
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
import typer
from .api import load_settings
from .http import SafeHTTP
from .storage import Storage
from .detection.service import Detector
from .downloads.manager import DownloadManager
from .models import DownloadRequest, MediaItem, MediaFormat

app = typer.Typer(help="Detect, inspect and download authorized website videos.", no_args_is_help=True)


@asynccontextmanager
async def services():
    settings = load_settings()
    settings.prepare()
    http = SafeHTTP(settings)
    storage = Storage(settings.database)
    try:
        yield Detector(settings, http), DownloadManager(settings, storage, http), storage
    finally:
        await http.close()


def display(result, json_output: bool) -> None:
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    typer.echo(f"Scan {result.scan_id}: {len(result.items)} media entries ({result.inspected_pages} pages)")
    for item in result.items:
        typer.echo(f"  [{item.id}] {item.title} · {item.extractor} · {len(item.formats)} formats")
        for f in item.formats[:18]:
            status = " ★ recommended" if f.recommended else (" ✓ verified" if f.accessibility == "response_ok" else "")
            typer.echo(f"    {f.id}: {f.quality or f.ext} / {f.protocol} / {f.backend}{status} · {f.url[:130]}")
    for warning in result.warnings:
        typer.echo(f"  Warning: {warning}", err=True)


async def scan_run(url: str, browser: bool = False, deep: bool = False):
    async with services() as (detector, _manager, storage):
        result = await detector.scan(url, browser_enabled=browser, deep=deep)
        storage.save_scan(result)
        return result


@app.command()
def scan(url: str, browser: bool = typer.Option(False, "--browser", help="Enable opt-in Chromium inspection"),
         deep: bool = typer.Option(False, "--deep", help="Run yt-dlp against inspected iframes too"),
         json_output: bool = typer.Option(False, "--json")):
    """Find video links and stream formats."""
    try:
        display(asyncio.run(scan_run(url, browser, deep)), json_output)
    except Exception as exc:
        typer.echo(f"Scan failed: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def info(url: str, browser: bool = False, json_output: bool = typer.Option(False, "--json")):
    """Show detailed media metadata for a page."""
    result = asyncio.run(scan_run(url, browser))
    display(result, json_output)


@app.command()
def formats(url: str, browser: bool = False, json_output: bool = typer.Option(False, "--json")):
    """List selectable format IDs, qualities and backend types."""
    result = asyncio.run(scan_run(url, browser))
    display(result, json_output)


def pick(result, quality: str | None, format_id: str | None, video_index: int, audio_only: bool):
    if not result.items:
        raise ValueError("No media found")
    if not 0 <= video_index < len(result.items):
        raise ValueError(f"Video index must be between 0 and {len(result.items) - 1}")
    item = result.items[video_index]
    if format_id:
        fmt = next((f for f in item.formats if f.id == format_id or f.selector == format_id), None)
        if not fmt:
            raise ValueError("Format id not found; run videograb formats first")
    elif quality:
        height = int(quality.lower().rstrip("p"))
        eligible = [f for f in item.formats if f.height is not None and f.height <= height and f.vcodec != "none"]
        if not eligible:
            higher = [f for f in item.formats if f.height is not None and f.vcodec != "none"]
            fmt = min(higher, key=lambda f: f.height or 0) if higher else item.formats[0]
        else:
            fmt = max(eligible, key=lambda f: f.height or 0)
    elif audio_only:
        fmt = next((f for f in item.formats if f.vcodec == "none"), item.formats[0])
    else:
        fmt = max(item.formats, key=lambda f: (f.height or 0, f.bitrate or 0))
    return item, fmt


async def download_run(url: str, quality: str | None, format_id: str | None,
                       audio_only: bool, video_index: int, browser: bool, json_output: bool, subtitles: bool):
    async with services() as (detector, manager, storage):
        result = await detector.scan(url, browser_enabled=browser)
        storage.save_scan(result)
        item, fmt = pick(result, quality, format_id, video_index, audio_only)
        job = await manager.enqueue(DownloadRequest(scan_id=result.scan_id, item_id=item.id,
                       format_id=fmt.id, audio_only=audio_only, subtitles=subtitles))
        job = await manager.run(job)
        typer.echo(job.model_dump_json(indent=2) if json_output else f"{job.status}: {job.output_path or job.error}")
        if job.status != "completed":
            raise typer.Exit(1)


@app.command()
def download(url: str, quality: str | None = typer.Option(None, "--quality"),
             format_id: str | None = typer.Option(None, "--format"),
             audio_only: bool = typer.Option(False, "--audio-only"),
             subtitles: bool = typer.Option(False, "--subtitles"),
             video_index: int = typer.Option(0, "--video-index"),
             browser: bool = typer.Option(False, "--browser"),
             json_output: bool = typer.Option(False, "--json")):
    """Download selected media. Video index is zero-based."""
    asyncio.run(download_run(url, quality, format_id, audio_only, video_index, browser, json_output, subtitles))


@app.command()
def batch(path: Path, quality: str | None = None, audio_only: bool = False,
          browser: bool = False):
    """Download each nonblank/noncomment URL from a text file."""
    urls = [s.strip() for s in path.read_text(encoding="utf-8").splitlines() if s.strip() and not s.startswith("#")]
    async def run():
        semaphore = asyncio.Semaphore(2)
        async def one(url):
            async with semaphore:
                try:
                    await download_run(url, quality, None, audio_only, 0, browser, False, False)
                except Exception as exc:
                    typer.echo(f"FAILED {url}: {exc}", err=True)
        await asyncio.gather(*(one(url) for url in urls))
    asyncio.run(run())


@app.command()
def jobs(json_output: bool = typer.Option(False, "--json")):
    """Show persisted download queue/history."""
    s = load_settings()
    s.prepare()
    data = Storage(s.database).list_jobs()
    if json_output:
        typer.echo(json.dumps([v.model_dump() for v in data], indent=2))
    else:
        for job in data:
            typer.echo(f"{job.id}  {job.status:12} {job.title[:45]}  {job.error or job.output_path or ''}")


@app.command()
def play(job_id: Annotated[str | None, typer.Argument(help="Job ID to play; defaults to most recent completed job")] = None):
    """Play or open a downloaded video in your default system media player."""
    s = load_settings()
    s.prepare()
    jobs = Storage(s.database).list_jobs()
    target_job = None
    if job_id:
        target_job = next((j for j in jobs if j.id == job_id), None)
        if not target_job:
            typer.echo(f"Job not found: {job_id}", err=True)
            raise typer.Exit(1)
    else:
        target_job = next((j for j in jobs if j.status == "completed" and j.output_path), None)
        if not target_job:
            typer.echo("No completed downloads found to play.", err=True)
            raise typer.Exit(1)
    if not target_job.output_path or not Path(target_job.output_path).is_file():
        typer.echo(f"Output file missing: {target_job.output_path}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Playing: {target_job.title} -> {target_job.output_path}")
    import subprocess
    import shutil
    opener = "xdg-open" if shutil.which("xdg-open") else ("open" if shutil.which("open") else None)
    if opener:
        subprocess.run([opener, target_job.output_path])
    else:
        typer.echo(f"Default opener unavailable. File is saved at: {target_job.output_path}")


@app.command()
def serve(host: str = typer.Option("127.0.0.1", help="Localhost binding only"),
          port: int = typer.Option(8765)):
    """Start local web UI at http://127.0.0.1:8765."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("Public interfaces are disallowed without authentication")
    import uvicorn
    from .api import create_app
    uvicorn.run(create_app(load_settings()), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
