"""SQLite persistence for scans and downloads. Connections are short-lived and thread safe."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .models import ScanResult, DownloadJob


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, json TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def save_scan(self, scan: ScanResult, max_records: int = 200) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?)", (scan.scan_id, scan.model_dump_json(), now()))
            # Bound history growth. Download jobs retain the snapshots they
            # need, so old scan-only records can be safely discarded.
            db.execute("""DELETE FROM scans WHERE id IN (
                SELECT id FROM scans ORDER BY created_at DESC LIMIT -1 OFFSET ?
            )""", (max(1, max_records),))

    def get_scan(self, scan_id: str) -> ScanResult | None:
        with self.connect() as db:
            result = db.execute("SELECT json FROM scans WHERE id=?", (scan_id,)).fetchone()
        return ScanResult.model_validate_json(result[0]) if result else None

    def save_job(self, job: DownloadJob) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)", (job.id, job.model_dump_json(), job.updated_at))

    def get_job(self, job_id: str) -> DownloadJob | None:
        with self.connect() as db:
            result = db.execute("SELECT json FROM jobs WHERE id=?", (job_id,)).fetchone()
        return DownloadJob.model_validate_json(result[0]) if result else None

    def list_jobs(self, limit: int = 200) -> list[DownloadJob]:
        with self.connect() as db:
            rows = db.execute("SELECT json FROM jobs ORDER BY updated_at DESC LIMIT ?", (min(limit, 500),)).fetchall()
        return [DownloadJob.model_validate_json(row[0]) for row in rows]

    def delete_job(self, job_id: str) -> bool:
        """Remove only the persisted job record, never its output file."""
        with self.connect() as db:
            return db.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount > 0

    def clear_finished_jobs(self) -> int:
        """Remove terminal job history while preserving active queue entries."""
        with self.connect() as db:
            return db.execute(
                "DELETE FROM jobs WHERE json_extract(json, '$.status') IN ('completed', 'failed', 'cancelled')"
            ).rowcount
