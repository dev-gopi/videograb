# VideoGrab — Universal Video Link Detector & Downloader

**Cross-platform Python 3.12+ application** with a shared detection/download core, CLI, responsive local FastAPI Web UI, job history, HTTP partial-file resume, optional Chromium network inspection, and yt-dlp/FFmpeg backends.

> **Practical compatibility, not a universal bypass.** DRM, paywalls, login barriers, CAPTCHA, inaccessible private players, encrypted HLS, expired signed links, geo-blocking, unsupported sites, and videos not exposed in ordinary browser traffic may be unavailable. Only download media you have permission to download.

## Install

Python 3.12+ and pip are required. FFmpeg/FFprobe are strongly recommended; Chromium is optional. Use a normal internet-connected environment to install packages. This project does not download dependencies on its own.

### Ubuntu / Debian

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg
cd videograb
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e '.[browser,test]'
playwright install chromium
videograb serve
```

Browse to **http://127.0.0.1:8765**. If Chromium has missing system libraries, run `playwright install --with-deps chromium` on a supported Linux desktop, or omit the browser extra and leave browser inspection off.

### Windows (PowerShell)

Install Python 3.12+ and a recent FFmpeg build; add both `python` and `ffmpeg` to PATH. In PowerShell:

```powershell
cd videograb
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[browser,test]"
playwright install chromium
videograb serve
```

Open **http://127.0.0.1:8765**. If PowerShell blocks activation, use `.\.venv\Scripts\python.exe -m pip install -e ".[browser,test]"` and `.\.venv\Scripts\videograb.exe serve` instead of changing system-wide script policies.

### Android / Termux

Install Termux from F-Droid or the official project channel. Install Python/FFmpeg:

```bash
pkg update && pkg upgrade
pkg install python ffmpeg clang libxml2 libxslt
termux-setup-storage
# Extract the project ZIP to ~/videograb
cd ~/videograb
python -m pip install -e .
export VIDEOGRAB_OUTPUT_DIR="$HOME/storage/downloads/VideoGrab"
# CLI works without Chromium; browser inspection requires a remote supported host.
videograb serve
```

Open **http://127.0.0.1:8765** in the Android browser. The browser UI works in Termux, but Playwright's packaged Chromium is generally **not available natively on Android**. For browser-level detection, run VideoGrab on Ubuntu/Windows and access it through an authenticated, trusted tunnel/proxy with security handled externally, or use a desktop copy; this build intentionally does not provide a public remote-browser endpoint. The localhost-only server cannot be reached from another device by default.

If `pip install -e .` requires a compiler or wheel unavailable in Termux, follow the current Termux package guidance for that dependency. Termux support is best effort and must be verified on your device/Android version.

## CLI usage

```bash
videograb scan "https://example.com/watch"
videograb scan "https://example.com/watch" --browser --deep --json
videograb info "https://example.com/watch"
videograb formats "https://example.com/watch" --json
videograb download "https://example.com/watch"
videograb download "https://example.com/watch" --quality 1080p
videograb download "https://example.com/watch" --format SOME_FORMAT_ID
videograb download "https://example.com/watch" --audio-only
videograb download "https://example.com/watch" --subtitles
videograb download "https://example.com/watch" --video-index 1
videograb batch urls.txt
videograb jobs --json
videograb serve --port 8765
```

`--video-index` is zero-based. `--quality 1080p` selects the best listed resolution not exceeding 1080p; if none is smaller, it picks the lowest known-quality alternative (see notes). The app shows each detected URL. Format `accessibility=detected` means it was found but not probed; `response_ok` means a media URL or manifest returned successful HTTP response headers. Neither status guarantees that all segments are accessible. A job is only successfully downloaded after it reaches `completed`.

## Detection pipeline

1. Validate and resolve the provided HTTP(S) URL; reject local/private/metadata IP addresses by default.
2. Fetch the page with bounded HTTPX reads, limited redirects, and per-hop URL validation.
3. Discover HTML5 sources, OpenGraph media, JSON-LD, inline JavaScript, and up to five external player scripts.
4. Recursively inspect up to three levels of accessible iframes (maximum 12 pages).
5. Request yt-dlp metadata and available formats for supported source pages, including separate audio/video and subtitles.
6. Expand common HLS master playlists and extract DASH representation metadata (best-effort).
7. If explicitly enabled, use headless Playwright Chromium to observe DOM media and browser media/network URLs while scrolling and attempting autoplay on existing `<video>` elements.
8. Merge results, deduplicate repeated media links where possible, and display all available media and warnings.
9. Select the format in the Web UI or CLI; download through native HTTP (progressive), yt-dlp (supported site extractors), or FFmpeg (streams/remux/audio conversion).

**Fast-first:** Chromium is never launched unless enabled per scan. To force yt-dlp on inspected iframe pages, use `--deep` or enable Deep iframe analysis in the UI. You can also scan a specific iframe URL on its own.

## Project layout

```text
src/videograb/
  api.py               FastAPI routes, localhost security and settings
  cli.py               Typer commands (same services)
  config.py            Environment configuration
  security.py          URL/DNS/host checks, filename sanitization
  http.py              Size-bounded HTTP client and redirect validation
  models.py            Typed API and task contracts
  storage.py           SQLite scan snapshots and download jobs
  detection/
    common.py          Media identifiers and MIME/extensions
    static.py          HTML, JavaScript and embedded player parsing
    manifests.py       HLS/DASH metadata inspection
    ytdlp.py           yt-dlp metadata extractor adapter
    browser.py         Opt-in Playwright/Chromium network/DOM stage
    service.py         Scan orchestration and extractor merging
  downloads/manager.py Async queue, retries, progress, HTTP/yt-dlp/FFmpeg
  web/index.html       Responsive self-contained local web interface
 tests/                Local, authorized fixture HTTP media server and tests
```

## Configuration

Environment settings are sampled on startup. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `VIDEOGRAB_DATA_DIR` | `~/.videograb` | SQLite database and saved UI preference |
| `VIDEOGRAB_OUTPUT_DIR` | `~/Downloads/VideoGrab` | Final media storage |
| `VIDEOGRAB_MAX_BYTES` | 4294967296 | Maximum file size, bytes (4 GiB) |
| `VIDEOGRAB_BROWSER` | `true` | Allow optional Chromium scan stage |
| `VIDEOGRAB_ALLOW_PRIVATE` | `false` | **Test only:** allow loopback/private URLs |

API routes: `POST /api/scan`, `GET /api/scans/{id}`, `POST /api/downloads`, `GET /api/downloads`, `GET /api/downloads/{id}`, `POST /api/downloads/{id}/{pause|resume|cancel}`, `GET /api/settings`, `PATCH /api/settings`, `GET /api/health`. Interactive docs: `/api/docs`. Use `Content-Type: application/json`. URL list for batch is a plain text file (one URL per line).

## Downloads and resume

Native progressive HTTP writes `*.part`; range-aware servers resume after interruption. Servers ignoring `Range` cause a clean restart. yt-dlp resumes supported files/fragments using its own native `.part` mechanism. FFmpeg stream conversion cannot reliably resume partial output and will start over on retry. Pause/cancel are cooperative and may not interrupt a blocking extractor immediately. Queued/in-progress jobs survive application restarts; paused jobs need manual resume from the UI/API. Job status includes received bytes, speed, ETA where available, and any error. Files receive unique stable job IDs in their names to avoid overwrites.

HLS quality and audio-group association are best handled by yt-dlp; the independent HLS parser provides variants, language hints and subtitle URLs when present. DASH metadata lists representations, but FFmpeg cannot always force a particular DASH representation; yt-dlp remains preferred when available. For complex fragmented media, codecs may be missing and some manifests may require custom headers/session cookies that this app cannot obtain. Audio-only extraction requires FFmpeg or yt-dlp's FFmpeg postprocessor. Subtitle download is supported through yt-dlp, and through independently detected subtitle URLs when requested with `--subtitles`.

## Test

```bash
pip install -e '.[test]'
pytest -q
```

Tests create **only local, generated, authorized sample videos**. Some cases require system FFmpeg; the opt-in browser fixture test is skipped when Playwright/Chromium is unavailable. No networked third-party sites are used in the automated suite. yt-dlp data mapping uses synthetic metadata; actual yt-dlp extractor success depends on the installed version, site and network availability. Test with media you are authorized to download.

## Troubleshooting & limitations

- **No results:** Try `--browser` on desktop, `--deep` for iframe extractors, or provide the embedded iframe page URL. Some sites require an interactive login or protected player (unsupported).
- **DRM/Widevine/PlayReady/FairPlay/encrypted media:** Not supported; never decrypted or circumvented.
- **403/404/expired URL:** Temporary signed URLs and referral restrictions can expire. Re-scan the page; yt-dlp re-extracts at download time. Authorization bypass isn't attempted.
- **FFmpeg not found:** Install it on the system path and restart the app. Segmented HLS/DASH and audio extraction require FFmpeg.
- **Chromium missing:** `pip install -e '.[browser]' && playwright install chromium`. Natively unavailable on many Termux installs.
- **Some HLS/DASH variants cannot be downloaded:** Manifests might refer to session-bound tracks, inaccessible segments or unsupported codecs. DASH variants shown in independent scanning are descriptive; yt-dlp's selector is more reliable.
- **Cookies/sign-in:** No cookie-import or credential management UI is provided and cookies are deliberately not persisted; authenticated sites may not work.
- **Network security:** This is a **single-user, loopback-only application**, not a multi-tenant SaaS. Requests enforce URL/scheme checks, reject private IP/DNS results, validate redirects, limit page sizes and concurrency. However, DNS may change between resolution and socket connect, and yt-dlp/FFmpeg/Chromium perform independent network connections; this is **not a hardened SSRF containment boundary**. Do not expose it publicly or feed it URLs from untrusted users. Consider OS network sandboxing or an egress proxy enforcing IP allowlists for server deployments.
- **Privacy:** Browser-inspected sites execute their own JavaScript in a sandboxed Chromium context, which may make normal third-party requests. Browser stage is opt-in and uses a fresh ephemeral session.

## License

MIT, see `LICENSE`. 0.1.0 is a functioning integrated local application with known compatibility and hardening limitations, **not** a guarantee of support for every site.

## Automated acceptance coverage (local only)

| Scenario | Verification |
|---|---|
| Direct MP4, including files larger than scan cap | Local test passed |
| HTML5 `<video>`, OpenGraph, inline/external JS sources | Local test passed |
| Accessible nested iframe videos and multiple page videos | Local test passed |
| HLS master/media playlists, variant metadata, subtitles, FFmpeg output | Local tests passed |
| DASH manifest metadata and FFmpeg output | Local tests passed |
| Redirects and progressive download/resume state, SQLite queue | Local tests passed |
| Separate audio/video via yt-dlp format selection | Offline adapter mock passed; real yt-dlp site untested |
| JavaScript-executed video source and network capture | Browser fixture prepared; skipped in environment without Chromium |
| Real websites supported by yt-dlp | Requires installation/network and authorized real-world test; not tested offline |

Run `pytest -q -rs` to identify environment-specific skips. Tests do not validate every site, Windows/Termux runtime, live signed URL, or DRM-related restriction.
