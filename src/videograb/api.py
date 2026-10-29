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
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
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


class CreateDirRequest(BaseModel):
    parent: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=128)


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

    @app.get("/api/scans/{scan_id}/preview/{item_id}/{format_id}")
    async def preview_media(scan_id: str, item_id: str, format_id: str, request: Request):
        scan = storage.get_scan(scan_id)
        if not scan:
            raise HTTPException(404, "Scan not found")
        item = next((i for i in scan.items if i.id == item_id), None)
        if not item:
            raise HTTPException(404, "Item not found in scan")
        fmt = next((f for f in item.formats if f.id == format_id), None)
        if not fmt:
            raise HTTPException(404, "Format not found")

        req_headers = {k: v for k, v in fmt.headers.items() if k.lower() in {"user-agent", "referer", "origin", "accept"}}
        if "range" in request.headers:
            req_headers["range"] = request.headers["range"]

        try:
            upstream_resp, final_url = await http.request(fmt.url, method="GET", headers=req_headers, stream=True)
        except Exception as exc:
            raise HTTPException(502, f"Failed to connect to stream: {exc}")

        import mimetypes
        content_type = upstream_resp.headers.get("content-type")
        if not content_type or "octet-stream" in content_type:
            content_type, _ = mimetypes.guess_type(final_url)
            if not content_type:
                content_type = "video/mp4"

        forward_headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Content-Disposition": "inline",
        }
        for h in ("content-range", "content-length"):
            if h in upstream_resp.headers:
                forward_headers[h] = upstream_resp.headers[h]

        async def stream_generator():
            try:
                async for chunk in upstream_resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=upstream_resp.status_code,
            headers=forward_headers,
            media_type=content_type,
        )

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

    @app.get("/api/downloads/{job_id}/file")
    async def media_file(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if not job.output_path:
            raise HTTPException(404, "Download has no output file")
        path = Path(job.output_path).resolve()
        if not path.is_file():
            raise HTTPException(404, "Downloaded file not found on disk")
        import mimetypes
        media_type, _ = mimetypes.guess_type(path.name)
        if not media_type:
            media_type = "audio/mpeg" if job.audio_only else "video/mp4"
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")

    @app.get("/api/downloads/{job_id}/stream")
    async def media_stream(job_id: str):
        return await media_file(job_id)

    @app.get("/api/downloads/{job_id}/subtitles")
    async def media_subtitles(job_id: str):
        job = storage.get_job(job_id)
        if not job or not job.output_path:
            return {"subtitles": []}
        path = Path(job.output_path).resolve()
        if not path.is_file():
            return {"subtitles": []}
        subtitles = []
        parent = path.parent
        stem = path.stem
        if parent.is_dir():
            for sub_file in parent.glob(f"{stem}.*"):
                if sub_file == path or not sub_file.is_file():
                    continue
                ext = sub_file.suffix.lower().lstrip(".")
                if ext in {"vtt", "srt"}:
                    parts = sub_file.name.split(".")
                    lang = parts[-2] if len(parts) >= 3 else "sub"
                    subtitles.append({
                        "language": lang,
                        "label": lang.upper(),
                        "url": f"/api/downloads/{job_id}/subtitles/{sub_file.name}",
                        "format": ext,
                    })
        return {"subtitles": subtitles}

    @app.get("/api/downloads/{job_id}/subtitles/{filename}")
    async def subtitle_file(job_id: str, filename: str):
        job = storage.get_job(job_id)
        if not job or not job.output_path:
            raise HTTPException(404, "Job not found")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "Invalid subtitle filename")
        path = (Path(job.output_path).parent / filename).resolve()
        if not path.is_file():
            raise HTTPException(404, "Subtitle file not found")
        media_type = "text/vtt" if path.suffix.lower() == ".vtt" else "text/plain"
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")

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

    @app.post("/api/downloads/{job_id}/open-folder")
    async def open_download_folder(job_id: str):
        job = storage.get_job(job_id)
        if not job or not job.output_path:
            raise HTTPException(404, "Download has no output file")
        path = Path(job.output_path).resolve()
        folder = path.parent
        if not folder.is_dir():
            raise HTTPException(404, "Directory does not exist")
        import shutil, subprocess
        opener = "xdg-open" if shutil.which("xdg-open") else ("open" if shutil.which("open") else None)
        opened = False
        if opener:
            try:
                subprocess.Popen([opener, str(folder)])
                opened = True
            except Exception:
                opened = False
        return {"opened": opened, "folder": str(folder)}

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

    @app.get("/api/fs/directories")
    async def list_fs_directories(path: str | None = None):
        target = Path(path).expanduser().resolve() if path else settings.output_dir.resolve()
        if not target.is_dir():
            target = settings.output_dir.resolve()
            if not target.is_dir():
                target = Path.home()
        home = Path.home()
        quick_links = [{"name": "Home", "path": str(home)}]
        for name in ("Downloads", "Videos", "Movies", "Documents", "Music", "Desktop"):
            cand = home / name
            if cand.is_dir() and str(cand) not in [q["path"] for q in quick_links]:
                quick_links.append({"name": name, "path": str(cand)})
        if str(settings.output_dir) not in [q["path"] for q in quick_links] and settings.output_dir.is_dir():
            quick_links.append({"name": "Current Output", "path": str(settings.output_dir)})

        subdirs = []
        try:
            for entry in sorted(target.iterdir(), key=lambda x: x.name.lower()):
                if entry.is_dir() and not entry.name.startswith("."):
                    subdirs.append({"name": entry.name, "path": str(entry)})
        except (PermissionError, OSError):
            pass

        return {
            "current": str(target),
            "parent": str(target.parent) if target.parent != target else None,
            "subdirs": subdirs[:250],
            "quick_links": quick_links,
        }

    @app.post("/api/fs/create-directory")
    async def create_fs_directory(data: CreateDirRequest):
        parent = Path(data.parent).expanduser().resolve()
        if not parent.is_dir():
            raise HTTPException(400, "Parent directory does not exist")
        name = data.name.strip()
        if "/" in name or "\\" in name or ".." in name or not name:
            raise HTTPException(400, "Invalid directory name")
        target = parent / name
        try:
            target.mkdir(parents=True, exist_ok=True)
            return {"path": str(target)}
        except Exception as exc:
            raise HTTPException(400, f"Could not create directory: {exc}") from exc

    @app.post("/api/settings/browse-native")
    async def browse_native_directory():
        import os, shutil, subprocess
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not has_display:
            return {"ok": False, "reason": "No display server available"}
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        chosen = None
        if zenity:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [zenity, "--file-selection", "--directory", "--title=Select Download Directory", f"--filename={settings.output_dir}/"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    chosen = proc.stdout.strip()
            except Exception:
                pass
        elif kdialog:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [kdialog, "--getexistingdirectory", str(settings.output_dir)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    chosen = proc.stdout.strip()
            except Exception:
                pass
        if chosen and Path(chosen).is_dir():
            return {"ok": True, "path": chosen}
        return {"ok": False, "reason": "Native directory picker unavailable or cancelled"}

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
