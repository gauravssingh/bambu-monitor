"""SQLite Database connection and PRAGMA management with WAL mode."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncGenerator
import aiosqlite

logger = logging.getLogger(__name__)

INIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS printers (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    serial_number TEXT UNIQUE NOT NULL,
    host TEXT NOT NULL,
    online INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    current_state_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id TEXT PRIMARY KEY,
    printer_id TEXT NOT NULL REFERENCES printers(id),
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    progress INTEGER NOT NULL DEFAULT 0,
    total_layers INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    FOREIGN KEY(printer_id) REFERENCES printers(id)
);
CREATE INDEX IF NOT EXISTS idx_print_jobs_printer_status ON print_jobs(printer_id, status);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    printer_id TEXT NOT NULL REFERENCES printers(id),
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT,
    details_json TEXT,
    FOREIGN KEY(printer_id) REFERENCES printers(id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_printer_status ON alerts(printer_id, status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    printer_id TEXT NOT NULL REFERENCES printers(id),
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(printer_id) REFERENCES printers(id)
);
CREATE INDEX IF NOT EXISTS idx_events_printer_timestamp ON events(printer_id, timestamp);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    printer_id TEXT NOT NULL REFERENCES printers(id),
    destination TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    delivered_at TEXT,
    error_message TEXT,
    FOREIGN KEY(printer_id) REFERENCES printers(id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_printer_status_id ON outbox(printer_id, status, id);
"""


class Database:
    """Database manager handling connections and WAL mode configuration."""

    def __init__(self, db_path: str = "./data/bambu.db"):
        self.db_path = db_path
        self._ensure_parent_dir()

    def _ensure_parent_dir(self) -> None:
        path = Path(self.db_path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

    async def get_connection(self) -> aiosqlite.Connection:
        """Create and configure a new connection with mandatory WAL PRAGMAs."""
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL;")
        await conn.execute("PRAGMA synchronous = NORMAL;")
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    async def init_db(self) -> None:
        """Initialize database tables and indexes."""
        conn = await self.get_connection()
        try:
            # Check journal mode
            async with conn.execute("PRAGMA journal_mode;") as cursor:
                row = await cursor.fetchone()
                journal_mode = row[0] if row else "unknown"
                logger.info("SQLite initialized with journal_mode: %s", journal_mode)

            await conn.executescript(INIT_SCHEMA_SQL)
            await conn.commit()
        finally:
            await conn.close()

    async def check_health(self) -> dict[str, Any]:
        """Check database accessibility and journal mode."""
        conn = await self.get_connection()
        try:
            async with conn.execute("PRAGMA journal_mode;") as cursor:
                row = await cursor.fetchone()
                journal_mode = row[0] if row else "unknown"
            return {
                "available": True,
                "journal_mode": str(journal_mode).lower(),
                "path": self.db_path,
            }
        except Exception as exc:
            logger.exception("Database health check failed: %s", exc)
            return {
                "available": False,
                "error": str(exc),
                "path": self.db_path,
            }
        finally:
            await conn.close()
