"""SQLite metadata store for recording sessions.

Two tables:
  - ``sessions``    — one row per record press (a cam0+cam1 take)
  - ``recordings``  — one row per camera recording, linked via ``session_id``
"""

import sqlite3
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DB_PATH, DB_PRAGMAS


class RecordingDB:
    """Manages the recordings metadata database."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DB_PATH
        self._conn: Optional[sqlite3.Connection] = None



    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # check_same_thread=False: the afl-camd daemon shares one
            # connection across its tick + HTTP threads (sqlite3 serializes
            # internally; writes in this codebase are short transactions)
            self._conn = sqlite3.connect(str(self.path),
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            for pragma, value in DB_PRAGMAS.items():
                self._conn.execute(f"PRAGMA {pragma} = {value}")
            self._init_schema()
            self._migrate()
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                camera TEXT NOT NULL,
                device_path TEXT NOT NULL,
                resolution TEXT NOT NULL,
                framerate INTEGER DEFAULT 30,
                duration_sec REAL,
                file_path TEXT NOT NULL,
                file_size_bytes INTEGER,
                codec TEXT DEFAULT 'MJPEG',
                container TEXT DEFAULT 'AVI',
                status TEXT DEFAULT 'complete',
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                folder TEXT,
                stitch_status TEXT NOT NULL DEFAULT 'none',
                notes TEXT
            );
        """)

    def _migrate(self):
        """Idempotent schema migration: add recordings.session_id and backfill.

        Legacy rows are paired into sessions by identical ``created_at``.
        Partial runs are safe: rows that already have a session_id are skipped.
        """
        cols = [r["name"] for r in self.conn.execute(
            "PRAGMA table_info(recordings)").fetchall()]
        if "session_id" not in cols:
            self.conn.execute("ALTER TABLE recordings ADD COLUMN session_id INTEGER")

        ungrouped = self.conn.execute(
            "SELECT id, created_at FROM recordings "
            "WHERE session_id IS NULL ORDER BY created_at").fetchall()
        if not ungrouped:
            self.conn.commit()
            return

        # Group by identical created_at (the implicit legacy pairing)
        groups: dict[str, list[int]] = {}
        for row in ungrouped:
            groups.setdefault(row["created_at"], []).append(row["id"])

        for created_at, ids in sorted(groups.items()):
            cur = self.conn.execute(
                "INSERT INTO sessions (created_at, stitch_status) VALUES (?, 'none')",
                (created_at,))
            session_id = cur.lastrowid
            for rid in ids:
                self.conn.execute(
                    "UPDATE recordings SET session_id = ? WHERE id = ?",
                    (session_id, rid))
        self.conn.commit()



    def add(self, camera: str, device_path: str, resolution: str,
            file_path: str, framerate: int = 30, status: str = "recording",
            notes: str = "", session_id: Optional[int] = None,
            created_at: Optional[str] = None, codec: str = "MJPEG",
            container: str = "AVI") -> int:
        """Insert a new recording row. Returns the new row id."""
        if session_id is not None:
            cursor = self.conn.execute(
                """INSERT INTO recordings
                   (camera, device_path, resolution, framerate, file_path,
                    status, notes, session_id, created_at, codec, container)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (camera, device_path, resolution, framerate, file_path,
                 status, notes, session_id, created_at, codec, container),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO recordings
                   (camera, device_path, resolution, framerate, file_path,
                    status, notes, codec, container)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (camera, device_path, resolution, framerate, file_path,
                 status, notes, codec, container),
            )
        self.conn.commit()
        return cursor.lastrowid

    def update_complete(self, recording_id: int, duration_sec: float,
                        file_size_bytes: int):
        """Mark a recording as complete with final stats."""
        self.conn.execute(
            """UPDATE recordings
               SET status = 'complete',
                   duration_sec = ?,
                   file_size_bytes = ?
               WHERE id = ?""",
            (duration_sec, file_size_bytes, recording_id),
        )
        self.conn.commit()

    def update_error(self, recording_id: int, notes: str = ""):
        """Mark a recording as errored."""
        self.conn.execute(
            "UPDATE recordings SET status = 'error', notes = ? WHERE id = ?",
            (notes, recording_id),
        )
        self.conn.commit()



    def add_session(self, created_at: str, folder: Optional[str] = None,
                    stitch_status: str = "none", notes: str = "") -> int:
        """Insert a new session row. Returns the new session id."""
        cursor = self.conn.execute(
            """INSERT INTO sessions (created_at, folder, stitch_status, notes)
               VALUES (?, ?, ?, ?)""",
            (created_at, folder, stitch_status, notes),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_session(self, session_id: int) -> Optional[dict]:
        """Get a single session by id."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List sessions with aggregate recording stats, newest first."""
        rows = self.conn.execute(
            """SELECT s.*,
                      COUNT(r.id) AS recording_count,
                      SUM(CASE WHEN r.status = 'complete'
                          THEN COALESCE(r.file_size_bytes, 0) ELSE 0 END) AS total_bytes,
                      MAX(CASE WHEN r.status = 'complete'
                          THEN COALESCE(r.duration_sec, 0) ELSE 0 END) AS total_duration,
                      MAX(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END) AS has_error
               FROM sessions s
               LEFT JOIN recordings r ON r.session_id = s.id
               GROUP BY s.id
               ORDER BY s.created_at DESC, s.id DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def live_recording_in_progress(self) -> Optional[dict]:
        """The newest live-stitch recording still marked 'recording' (with
        its session folder): one a power cut interrupted."""
        row = self.conn.execute(
            """SELECT r.id AS recording_id, r.session_id, s.folder
               FROM recordings r JOIN sessions s ON s.id = r.session_id
               WHERE r.camera = 'stitch' AND r.status = 'recording'
               ORDER BY r.id DESC LIMIT 1""").fetchone()
        return dict(row) if row else None

    def session_recordings(self, session_id: int) -> list[dict]:
        """Recordings of one session, ordered by camera."""
        rows = self.conn.execute(
            "SELECT * FROM recordings WHERE session_id = ? ORDER BY camera",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_session_stitch_status(self, session_id: int, status: str,
                                     notes: str = ""):
        """Set a session's stitch status; notes replace the stored value."""
        if notes:
            self.conn.execute(
                "UPDATE sessions SET stitch_status = ?, notes = ? WHERE id = ?",
                (status, notes, session_id),
            )
        else:
            self.conn.execute(
                "UPDATE sessions SET stitch_status = ? WHERE id = ?",
                (status, session_id),
            )
        self.conn.commit()

    def session_folder(self, session_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT folder FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row["folder"] if row else None

    def set_session_folder(self, session_id: int, folder: str):
        self.conn.execute(
            "UPDATE sessions SET folder = ? WHERE id = ?", (folder, session_id),
        )
        self.conn.commit()

    def delete_session(self, session_id: int):
        """Delete a session and its recording rows (one transaction).

        Does not delete files — callers must remove the folder first.
        """
        self.conn.execute("DELETE FROM recordings WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()


    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List recent recordings, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM recordings ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, recording_id: int) -> Optional[dict]:
        """Get a single recording by id."""
        row = self.conn.execute(
            "SELECT * FROM recordings WHERE id = ?", (recording_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete(self, recording_id: int):
        """Delete a recording row (does not delete the file)."""
        self.conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        self.conn.commit()



    def file_size_fmt(self, size_bytes: Optional[int]) -> str:
        """Human-readable file size."""
        if size_bytes is None:
            return "?"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(size_bytes) < 1024:
                return f"{size_bytes:.0f}{unit}"
            size_bytes /= 1024
        return f"{size_bytes:.0f}PB"
