"""
Transcript Agent — Job History & Checkpoint Store
SQLite-backed. File: transcript_history.db inside the outputs directory.

Provides:
  - Persistent job history browsable across sessions
  - Whisper checkpoint: raw text is saved after transcription so Claude
    can be retried without re-running Whisper if something goes wrong
  - File-hash deduplication: re-uploading the same file reuses the checkpoint
"""

import sqlite3
import hashlib
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

_DB_PATH: Optional[Path] = None


def init_db(outputs_dir) -> Path:
    global _DB_PATH
    _DB_PATH = Path(outputs_dir) / "transcript_history.db"
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id             TEXT UNIQUE NOT NULL,
                stem               TEXT,
                original_filename  TEXT,
                file_hash          TEXT,
                status             TEXT DEFAULT 'pending',
                created_at         TEXT,
                updated_at         TEXT,
                job_dir            TEXT,
                whisper_text       TEXT,
                whisper_json       TEXT,
                result_summary     TEXT,
                result_transcript  TEXT,
                result_dialogue    TEXT,
                result_profiles    TEXT,
                result_analytics   TEXT,
                result_combined    TEXT,
                config_json        TEXT,
                error              TEXT,
                panel_mode         INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_file_hash ON jobs(file_hash);
            CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_created   ON jobs(created_at);
        """)
    # Migrate existing DBs that predate the panel_mode column
    try:
        with _conn() as c:
            c.execute("ALTER TABLE jobs ADD COLUMN panel_mode INTEGER DEFAULT 0")
    except Exception:
        pass  # column already exists
    return _DB_PATH


@contextmanager
def _conn():
    if _DB_PATH is None:
        raise RuntimeError("call init_db() before using job_db")
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_job(job_id: str, stem: str, original_filename: str,
               file_hash: str, job_dir: str, config_json: str = "",
               panel_mode: bool = False) -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO jobs
              (job_id, stem, original_filename, file_hash,
               status, created_at, updated_at, job_dir, config_json, panel_mode)
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
        """, (job_id, stem, original_filename, file_hash, now, now, job_dir, config_json,
              1 if panel_mode else 0))


def save_whisper_checkpoint(job_id: str, whisper_text: str,
                             whisper_json: str = "") -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute("""
            UPDATE jobs
            SET whisper_text=?, whisper_json=?, status='whisper_done', updated_at=?
            WHERE job_id=?
        """, (whisper_text, whisper_json, now, job_id))


def find_whisper_checkpoint(file_hash: str, panel_mode: bool = False) -> Optional[tuple]:
    """Return (whisper_text, whisper_json_str) if a matching checkpoint exists, else None.

    panel_mode must match the original run's mode so a diarized checkpoint is not
    reused for a standard run (or vice versa), which would cause silent output degradation.
    """
    if not file_hash:
        return None
    with _conn() as c:
        row = c.execute("""
            SELECT whisper_text, whisper_json FROM jobs
            WHERE file_hash=?
              AND panel_mode=?
              AND whisper_text IS NOT NULL
              AND whisper_text != ''
            ORDER BY updated_at DESC LIMIT 1
        """, (file_hash, 1 if panel_mode else 0)).fetchone()
    return (row["whisper_text"], row["whisper_json"]) if row else None


def complete_job(job_id: str, result_data: dict) -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute("""
            UPDATE jobs
            SET status='done', updated_at=?,
                result_summary=?, result_transcript=?, result_dialogue=?,
                result_profiles=?, result_analytics=?, result_combined=?
            WHERE job_id=?
        """, (
            now,
            result_data.get("summary", ""),
            result_data.get("transcript", ""),
            result_data.get("dialogue", ""),
            result_data.get("profiles", ""),
            result_data.get("analytics", ""),
            result_data.get("combined", ""),
            job_id,
        ))


def fail_job(job_id: str, error: str) -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute("""
            UPDATE jobs SET status='error', error=?, updated_at=? WHERE job_id=?
        """, (error[:500], now, job_id))


def get_job(job_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 100) -> list:
    with _conn() as c:
        rows = c.execute("""
            SELECT job_id, stem, original_filename, status,
                   created_at, updated_at, job_dir, error
            FROM jobs ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))


# ── Utility ───────────────────────────────────────────────────────────────────

def file_md5(path: str) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""
