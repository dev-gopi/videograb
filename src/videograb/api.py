"""Localhost-only FastAPI server; all operations share Detector/Storage/Manager."""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from .config import Settings
from .http import SafeHTTP
from .storage import Storage
from .models import ScanRequest, DownloadRequest, BulkDownloadRequest
from .detection.service import Detector
from .downloads.manager import DownloadManager

log = logging.getLogger(__name__)


class SettingsUpdate(BaseModel):
    output_dir: str = Field(min_length=1, max_length=1024)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.prepare()
    storage = Storage(settings.database)
    http = SafeHTTP(settings)
    detector = Detector(settings, http)
    manager = DownloadManager(settings, storage, http)
    scan_tasks: dict[str, asyncio.Task] = {}
    scan_gate = asyncio.Semaphore(settings.scan_workers)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            for task in scan_tasks.values():
                task.cancel()
            if scan_tasks:
                await asyncio.gather(*scan_tasks.values(), return_exceptions=True)
            await manager.stop()
            await http.close()

    app = FastAPI(title="VideoGrab", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.services = (detector, manager, storage)

    @app.middleware("http")
    async def basic_security(request: Request, call_next):
        # Browsers from another origin cannot issue JSON POST without CORS preflight.
        # Protect against DNS rebinding targeting the local server itself.
        raw_host = request.headers.get("host", "").lower()
        # ``Host`` uses brackets for IPv6, e.g. ``[::1]:8765``.  Splitting
        # blindly on ':' turns that value into '[' and made the documented
        # IPv6 localhost mode unusable.
        if raw_host.startswith("["):
            host = raw_host.split("]", 1)[0] + "]"
        else:
            host = raw_host.rsplit(":", 1)[0] if raw_host.count(":") == 1 else raw_host
        if host not in {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"}:
            return HTMLResponse("Invalid Host header", status_code=403)
        if request.method in {"POST", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlsplit
                parsed = urlsplit(origin)
                if parsed.netloc != request.headers.get("host") or parsed.scheme not in {"http", "https"}:
                    return HTMLResponse("Cross-origin mutations rejected", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return HTMLResponse((Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": "0.1.0"}

    @app.post("/api/scan")
    async def scan(data: ScanRequest, request: Request):
        if len(data.url) > 2048:
            raise HTTPException(400, "URL too long")
        scan_token = request.headers.get("x-videograb-scan-id")
        if scan_token and not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", scan_token):
            raise HTTPException(400, "Invalid scan identifier")
        if scan_token and scan_token in scan_tasks:
            raise HTTPException(409, "A scan with this identifier is already active")
        async def limited_scan():
            async with scan_gate:
                return await detector.scan(data.url, browser_enabled=data.browser, deep=data.deep)
        task = asyncio.create_task(limited_scan())
        if scan_token:
            scan_tasks[scan_token] = task
        try:
            result = await task
            storage.save_scan(result, settings.max_saved_scans)
            return result
        except asyncio.CancelledError as exc:
            raise HTTPException(499, "Scan cancelled") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            if scan_token and scan_tasks.get(scan_token) is task:
                scan_tasks.pop(scan_token, None)

    @app.post("/api/scans/{scan_token}/cancel", status_code=202)
    async def cancel_scan(scan_token: str):
        task = scan_tasks.get(scan_token)
        if not task or task.done():
            raise HTTPException(404, "Active scan not found")
        task.cancel()
        return {"cancelled": True}

    @app.get("/api/scans/{scan_id}")
    async def scan_details(scan_id: str):
        result = storage.get_scan(scan_id)
        if not result:
            raise HTTPException(404, "Scan not found")
        return result

    @app.post("/api/downloads", status_code=202)
    async def download(selection: DownloadRequest):
        try:
            return await manager.enqueue(selection)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/downloads/bulk", status_code=202)
    async def bulk_download(data: BulkDownloadRequest):
        jobs = []
        errors = []
        for selection in data.selections:
            try:
                jobs.append(await manager.enqueue(selection))
            except ValueError as exc:
                errors.append({"item_id": selection.item_id, "error": str(exc)})
        return {"jobs": jobs, "errors": errors}

    @app.get("/api/downloads")
    async def history():
        return storage.list_jobs()

    @app.get("/api/downloads/{job_id}")
    async def details(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    @app.delete("/api/downloads/{job_id}", status_code=204)
    async def delete_history_item(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.status in {"queued", "downloading", "paused"}:
            raise HTTPException(409, "Pause or cancel an active job before deleting it")
        storage.delete_job(job_id)

    @app.delete("/api/downloads")
    async def clear_history():
        return {"deleted": storage.clear_finished_jobs()}

    @app.post("/api/downloads/{job_id}/{action}")
    async def control(job_id: str, action: str):
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(404, "Unknown action")
        try:
            return await manager.control(job_id, action)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/settings")
    async def get_settings():
        import importlib.util
        import shutil
        return {"output_dir": str(settings.output_dir), "max_bytes": settings.max_download_bytes,
                "workers": settings.download_workers, "browser_available": settings.browser_enabled,
                "playwright_installed": importlib.util.find_spec("playwright") is not None,
                "ffmpeg_available": bool(shutil.which("ffmpeg")), "private_networks_allowed": settings.allow_private_networks}

    @app.patch("/api/settings")
    async def update_settings(data: SettingsUpdate):
        path = Path(data.output_dir).expanduser()
        if not path.is_absolute():
            raise HTTPException(400, "Output directory must be an absolute path")
        try:
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise ValueError("Not a directory")
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"Invalid output directory: {exc}") from exc
        settings.output_dir = path
        (settings.data_dir / "preferences.json").write_text(json.dumps({"output_dir": str(path)}), encoding="utf-8")
        return {"output_dir": str(path)}

    return app


def load_settings() -> Settings:
    settings = Settings()
    prefs = settings.data_dir / "preferences.json"
    if prefs.exists():
        try:
            stored = json.loads(prefs.read_text(encoding="utf-8"))
            if isinstance(stored.get("output_dir"), str) and Path(stored["output_dir"]).is_absolute():
                settings.output_dir = Path(stored["output_dir"])
        except (OSError, ValueError, KeyError):
            pass
    return settings
