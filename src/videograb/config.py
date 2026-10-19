"""Settings are read once for each application instance; CLI and API share them."""
from __future__ import annotations
import os
import math
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    """Read a bounded integer setting without making app startup fragile."""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return max(minimum, value) if math.isfinite(value) else default
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("VIDEOGRAB_DATA_DIR", str(Path.home() / ".videograb"))).expanduser())
    output_dir: Path = field(default_factory=lambda: Path(os.getenv("VIDEOGRAB_OUTPUT_DIR", str(Path.home() / "Downloads" / "VideoGrab"))).expanduser())
    allow_private_networks: bool = field(default_factory=lambda: _flag("VIDEOGRAB_ALLOW_PRIVATE", False))
    max_download_bytes: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_MAX_BYTES", 4 * 1024**3, 1024))
    max_scan_bytes: int = 2 * 1024 * 1024
    max_redirects: int = 5
    max_iframe_depth: int = 3
    max_pages: int = 12
    max_scripts: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_MAX_SCRIPTS", 12))
    max_items: int = 60
    max_saved_scans: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_MAX_SAVED_SCANS", 200))
    scan_workers: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_SCAN_WORKERS", 2))
    max_verify_formats: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_MAX_VERIFY_FORMATS", 12))
    verify_timeout: float = field(default_factory=lambda: _positive_float("VIDEOGRAB_VERIFY_TIMEOUT", 5.0))
    download_workers: int = 2
    request_timeout: float = 15.0
    browser_timeout_ms: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_BROWSER_TIMEOUT_MS", 30000, 1000))
    browser_scrolls: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_BROWSER_SCROLLS", 5))
    browser_settle_ms: int = field(default_factory=lambda: _positive_int("VIDEOGRAB_BROWSER_SETTLE_MS", 2500, 250))
    browser_enabled: bool = field(default_factory=lambda: _flag("VIDEOGRAB_BROWSER", True))

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def database(self) -> Path:
        return self.data_dir / "history.sqlite3"
