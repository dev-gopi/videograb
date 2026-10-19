# VideoGrab v0.1.0 — Local Validation Report

Date: 2026-10-17
Environment: Linux, Python 3.13.5, system FFmpeg/FFprobe installed, FastAPI/HTTPX/Typer/Playwright Python library available. Network access for installing additional packages is unavailable.

Command: `PYTHONPATH=src pytest -q -rs`

**Results: 12 passed, 1 skipped**.

**Passed:** local HTTP server fixture end-to-end scanning HTML5, OpenGraph and video URLs in inline/external JavaScript; multiple videos and nested accessible iframe; direct MP4 >2 MiB; redirected video link; HLS master variant/subtitle metadata, FFmpeg HLS and subtitle output; DASH representation metadata and FFmpeg download; SQLite task queue, persisted history, pause/resume/cancel transitions and duplicate avoidance; FastAPI UI and JSON endpoints; CLI scan/download/jobs; SSRF blocking for private/loopback addresses by default; synthetic yt-dlp info mapping and downloader format selector with separate audio/video.

**Skipped:** Playwright dynamic-page browser test: Chromium browser binary not installed. Python Playwright integration is wired, but was not executed in this environment.

**Not independently verified here:** actual third-party yt-dlp extractors and authenticated media websites (yt-dlp could not be installed without network); Windows and Android/Termux runtime installations; DRM-restricted content (out of scope), and browser network inspection of real external websites.

**Additional checks:** Python source compiles (`compileall`); JavaScript Web UI passes `node --check`; editable installation using installed dependencies and `--no-deps --no-build-isolation` passed; `setuptools` wheel builds successfully and includes `web/index.html`.

This report does not claim universal site compatibility or hardened public-server SSRF isolation. For single-user localhost operation only; see README security and limitation sections.
