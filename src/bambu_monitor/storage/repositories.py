"""Repositories for SQLite data access."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bambu_monitor.domain.alerts import Alert
from bambu_monitor.domain.events import DomainEvent, OutboxMessage, OutboxStatus
from bambu_monitor.domain.printer import CurrentPrinterState, Printer
from bambu_monitor.domain.print_job import PrintJob
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.models import (
    format_datetime,
    row_to_alert,
    row_to_domain_event,
    row_to_outbox_message,
    row_to_print_job,
    row_to_printer,
)

logger = logging.getLogger(__name__)


class PrinterRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, printer: Printer, current_state: Optional[CurrentPrinterState] = None) -> None:
        state_json = current_state.model_dump_json() if current_state else None
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                INSERT INTO printers (id, model, serial_number, host, online, last_seen, current_state_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    model = excluded.model,
                    serial_number = excluded.serial_number,
                    host = excluded.host,
                    online = excluded.online,
                    last_seen = excluded.last_seen,
                    current_state_json = coalesce(excluded.current_state_json, printers.current_state_json),
                    updated_at = excluded.updated_at
                """,
                (
                    printer.id,
                    printer.model,
                    printer.serial_number,
                    printer.host,
                    1 if printer.online else 0,
                    format_datetime(printer.last_seen),
                    state_json,
                    format_datetime(printer.updated_at),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get(self, printer_id: str) -> Optional[Printer]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)) as cursor:
                row = await cursor.fetchone()
                return row_to_printer(row) if row else None
        finally:
            await conn.close()

    async def list_all(self) -> List[Printer]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT * FROM printers ORDER BY id") as cursor:
                rows = await cursor.fetchall()
                return [row_to_printer(r) for r in rows]
        finally:
            await conn.close()

    async def update_current_state(self, printer_id: str, state: CurrentPrinterState) -> None:
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                UPDATE printers
                SET online = ?, last_seen = ?, current_state_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    1 if state.online else 0,
                    format_datetime(state.last_seen),
                    state.model_dump_json(),
                    format_datetime(state.updated_at),
                    printer_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_current_state(self, printer_id: str) -> Optional[CurrentPrinterState]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT current_state_json FROM printers WHERE id = ?", (printer_id,)) as cursor:
                row = await cursor.fetchone()
                if row and row["current_state_json"]:
                    return CurrentPrinterState.model_validate_json(row["current_state_json"])
                return None
        finally:
            await conn.close()


class JobRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, job: PrintJob) -> None:
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                INSERT INTO print_jobs (id, printer_id, filename, status, started_at, completed_at, duration_seconds, progress, total_layers, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    duration_seconds = excluded.duration_seconds,
                    progress = excluded.progress,
                    total_layers = excluded.total_layers,
                    metadata_json = excluded.metadata_json
                """,
                (
                    job.id,
                    job.printer_id,
                    job.filename,
                    job.status.value,
                    format_datetime(job.started_at),
                    format_datetime(job.completed_at),
                    job.duration_seconds,
                    job.progress,
                    job.total_layers,
                    json.dumps(job.metadata),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get(self, job_id: str) -> Optional[PrintJob]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)) as cursor:
                row = await cursor.fetchone()
                return row_to_print_job(row) if row else None
        finally:
            await conn.close()

    async def get_active_for_printer(self, printer_id: str) -> Optional[PrintJob]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM print_jobs
                WHERE printer_id = ? AND status IN ('prepare', 'running', 'paused')
                ORDER BY started_at DESC LIMIT 1
                """,
                (printer_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return row_to_print_job(row) if row else None
        finally:
            await conn.close()

    async def list_for_printer(self, printer_id: str, limit: int = 50) -> List[PrintJob]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM print_jobs WHERE printer_id = ? ORDER BY started_at DESC LIMIT ?",
                (printer_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row_to_print_job(r) for r in rows]
        finally:
            await conn.close()


class AlertRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, alert: Alert) -> None:
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                INSERT INTO alerts (id, printer_id, alert_type, severity, status, created_at, acknowledged_at, resolved_at, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    severity = excluded.severity,
                    status = excluded.status,
                    acknowledged_at = excluded.acknowledged_at,
                    resolved_at = excluded.resolved_at,
                    details_json = excluded.details_json
                """,
                (
                    alert.id,
                    alert.printer_id,
                    alert.alert_type,
                    alert.severity.value,
                    alert.status.value,
                    format_datetime(alert.created_at),
                    format_datetime(alert.acknowledged_at),
                    format_datetime(alert.resolved_at),
                    json.dumps(alert.details),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get(self, alert_id: str) -> Optional[Alert]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)) as cursor:
                row = await cursor.fetchone()
                return row_to_alert(row) if row else None
        finally:
            await conn.close()

    async def get_active_by_type(self, printer_id: str, alert_type: str) -> Optional[Alert]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM alerts
                WHERE printer_id = ? AND alert_type = ? AND status IN ('active', 'acknowledged')
                ORDER BY created_at DESC LIMIT 1
                """,
                (printer_id, alert_type),
            ) as cursor:
                row = await cursor.fetchone()
                return row_to_alert(row) if row else None
        finally:
            await conn.close()

    async def list_active(self, printer_id: str) -> List[Alert]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM alerts
                WHERE printer_id = ? AND status IN ('active', 'acknowledged')
                ORDER BY created_at DESC
                """,
                (printer_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row_to_alert(r) for r in rows]
        finally:
            await conn.close()

    async def list_all(self, printer_id: str, limit: int = 50) -> List[Alert]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM alerts WHERE printer_id = ? ORDER BY created_at DESC LIMIT ?",
                (printer_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row_to_alert(r) for r in rows]
        finally:
            await conn.close()


class EventRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, event: DomainEvent) -> None:
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                INSERT INTO events (event_id, printer_id, event_type, severity, timestamp, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.printer_id,
                    event.event_type,
                    event.severity.value,
                    format_datetime(event.timestamp),
                    json.dumps(event.payload),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def save_and_enqueue(self, event: DomainEvent, destination: str) -> None:
        """Atomically persist an event and its durable delivery work item."""
        conn = await self.db.get_connection()
        try:
            await conn.execute("BEGIN")
            await conn.execute(
                """
                INSERT INTO events (event_id, printer_id, event_type, severity, timestamp, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.printer_id,
                    event.event_type,
                    event.severity.value,
                    format_datetime(event.timestamp),
                    json.dumps(event.payload),
                ),
            )
            await conn.execute(
                """
                INSERT INTO outbox (event_id, printer_id, destination, payload_json, status, attempts, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?)
                ON CONFLICT(event_id, destination) DO NOTHING
                """,
                (
                    event.event_id,
                    event.printer_id,
                    destination,
                    json.dumps(event.model_dump(mode="json")),
                    format_datetime(event.timestamp),
                ),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()

    async def get_by_event_id(self, event_id: str) -> Optional[DomainEvent]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)) as cursor:
                row = await cursor.fetchone()
                return row_to_domain_event(row) if row else None
        finally:
            await conn.close()

    async def list_for_printer(
        self,
        printer_id: str,
        limit: int = 50,
        since: Optional[datetime] = None,
    ) -> List[DomainEvent]:
        conn = await self.db.get_connection()
        try:
            if since:
                query = """
                    SELECT * FROM events
                    WHERE printer_id = ? AND timestamp >= ?
                    ORDER BY timestamp DESC LIMIT ?
                """
                params = (printer_id, format_datetime(since), limit)
            else:
                query = "SELECT * FROM events WHERE printer_id = ? ORDER BY timestamp DESC LIMIT ?"
                params = (printer_id, limit)

            async with conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [row_to_domain_event(r) for r in rows]
        finally:
            await conn.close()


class OutboxRepository:
    def __init__(self, db: Database):
        self.db = db

    async def enqueue(self, event: DomainEvent, destination: str) -> OutboxMessage:
        conn = await self.db.get_connection()
        try:
            payload_json = json.dumps(event.model_dump(mode="json"))
            now_str = format_datetime(event.timestamp)
            cursor = await conn.execute(
                """
                INSERT INTO outbox (event_id, printer_id, destination, payload_json, status, attempts, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?)
                ON CONFLICT(event_id, destination) DO NOTHING
                """,
                (event.event_id, event.printer_id, destination, payload_json, now_str),
            )
            # rowcount == 0 means the conflict guard suppressed the insert:
            # the event was already enqueued, so no new work item was created.
            msg_id = cursor.lastrowid if cursor.rowcount else None
            await conn.commit()
            return OutboxMessage(
                id=msg_id,
                event_id=event.event_id,
                printer_id=event.printer_id,
                destination=destination,
                payload=event.model_dump(mode="json"),
                status=OutboxStatus.PENDING,
                attempts=0,
                created_at=event.timestamp,
            )
        finally:
            await conn.close()

    async def list_pending(self, printer_id: Optional[str] = None, limit: int = 50) -> List[OutboxMessage]:
        conn = await self.db.get_connection()
        try:
            if printer_id:
                query = "SELECT * FROM outbox WHERE printer_id = ? AND status = 'pending' ORDER BY id ASC LIMIT ?"
                params = (printer_id, limit)
            else:
                query = "SELECT * FROM outbox WHERE status = 'pending' ORDER BY id ASC LIMIT ?"
                params = (limit,)

            async with conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [row_to_outbox_message(r) for r in rows]
        finally:
            await conn.close()

    async def peek_next_for_printer(self, printer_id: str) -> Optional[OutboxMessage]:
        """Fetch the oldest non-delivered message for a printer (FIFO)."""
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM outbox WHERE printer_id = ? AND status IN ('pending', 'delivering') ORDER BY id ASC LIMIT 1",
                (printer_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return row_to_outbox_message(row) if row else None
        finally:
            await conn.close()

    async def begin_delivery(self, message_id: int, attempts: int, last_attempt_at: datetime) -> bool:
        """Atomically claim a pending message for delivery.

        Returns True only if this caller performed the pending -> delivering
        transition (compare-and-set), preventing concurrent double delivery.
        """
        conn = await self.db.get_connection()
        try:
            cursor = await conn.execute(
                """
                UPDATE outbox
                SET status = 'delivering', attempts = ?, last_attempt_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (attempts, format_datetime(last_attempt_at), message_id),
            )
            await conn.commit()
            return bool(cursor.rowcount)
        finally:
            await conn.close()

    async def reclaim_stale_delivering(self, stale_after_seconds: float) -> int:
        """Reset 'delivering' rows whose last attempt is older than the cutoff
        back to 'pending' (crash recovery: the delivering outcome is unknown)."""
        conn = await self.db.get_connection()
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - stale_after_seconds
            cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
            cursor = await conn.execute(
                """
                UPDATE outbox
                SET status = 'pending'
                WHERE status = 'delivering'
                  AND last_attempt_at IS NOT NULL
                  AND last_attempt_at < ?
                """,
                (format_datetime(cutoff_dt),),
            )
            await conn.commit()
            if cursor.rowcount:
                logger.warning(
                    "Reclaimed %d stale 'delivering' outbox message(s) after crash/timeout",
                    cursor.rowcount,
                )
            return cursor.rowcount or 0
        finally:
            await conn.close()

    async def update_status(
        self,
        message_id: int,
        status: OutboxStatus,
        attempts: Optional[int] = None,
        last_attempt_at: Optional[datetime] = None,
        delivered_at: Optional[datetime] = None,
        error_message: Optional[str] = None,
    ) -> None:
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                UPDATE outbox
                SET status = ?,
                    attempts = coalesce(?, attempts),
                    last_attempt_at = coalesce(?, last_attempt_at),
                    delivered_at = coalesce(?, delivered_at),
                    error_message = coalesce(?, error_message)
                WHERE id = ?
                """,
                (
                    status.value,
                    attempts,
                    format_datetime(last_attempt_at),
                    format_datetime(delivered_at),
                    error_message,
                    message_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_counts(self) -> Dict[str, Any]:
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT status, count(*) as cnt FROM outbox GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                counts = {r["status"]: r["cnt"] for r in rows}
            return {
                "pending": counts.get("pending", 0),
                "delivering": counts.get("delivering", 0),
                "delivered": counts.get("delivered", 0),
                "failed_dlq": counts.get("failed", 0),
            }
        finally:
            await conn.close()


class TimelapseRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save_session(self, session: Any) -> None:
        from bambu_monitor.storage.models import format_datetime
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                INSERT INTO timelapse_sessions (
                    id, print_job_id, printer_id, camera_id, camera_type, status,
                    started_at, paused_at, resumed_at, completed_at, frame_count,
                    missed_frames, capture_interval_seconds, video_fps, video_path,
                    storage_dir, error, paused_seconds, camera_outage_count,
                    camera_outage_seconds, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    paused_at = excluded.paused_at,
                    resumed_at = excluded.resumed_at,
                    completed_at = excluded.completed_at,
                    frame_count = excluded.frame_count,
                    missed_frames = excluded.missed_frames,
                    video_path = excluded.video_path,
                    error = excluded.error,
                    paused_seconds = excluded.paused_seconds,
                    camera_outage_count = excluded.camera_outage_count,
                    camera_outage_seconds = excluded.camera_outage_seconds,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session.id,
                    session.print_job_id,
                    session.printer_id,
                    session.camera_id,
                    session.camera_type,
                    session.status.value if hasattr(session.status, "value") else str(session.status),
                    format_datetime(session.started_at),
                    format_datetime(session.paused_at),
                    format_datetime(session.resumed_at),
                    format_datetime(session.completed_at),
                    session.frame_count,
                    session.missed_frames,
                    session.capture_interval_seconds,
                    session.video_fps,
                    session.video_path,
                    session.storage_dir,
                    session.error,
                    session.paused_seconds,
                    session.camera_outage_count,
                    session.camera_outage_seconds,
                    json.dumps(session.metadata),
                    format_datetime(session.created_at),
                    format_datetime(session.updated_at),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_session(self, session_id: str) -> Optional[Any]:
        from bambu_monitor.storage.models import row_to_timelapse_session
        conn = await self.db.get_connection()
        try:
            async with conn.execute("SELECT * FROM timelapse_sessions WHERE id = ?", (session_id,)) as cursor:
                row = await cursor.fetchone()
                return row_to_timelapse_session(row) if row else None
        finally:
            await conn.close()

    async def get_session_by_job(self, job_id: str) -> Optional[Any]:
        from bambu_monitor.storage.models import row_to_timelapse_session
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM timelapse_sessions WHERE print_job_id = ? ORDER BY started_at DESC LIMIT 1",
                (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return row_to_timelapse_session(row) if row else None
        finally:
            await conn.close()

    async def get_active_session_for_printer(self, printer_id: str) -> Optional[Any]:
        from bambu_monitor.storage.models import row_to_timelapse_session
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM timelapse_sessions
                WHERE printer_id = ? AND status IN ('idle', 'capturing', 'paused', 'degraded', 'finalizing')
                ORDER BY started_at DESC LIMIT 1
                """,
                (printer_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return row_to_timelapse_session(row) if row else None
        finally:
            await conn.close()

    async def list_sessions_for_printer(self, printer_id: str, limit: int = 50) -> List[Any]:
        from bambu_monitor.storage.models import row_to_timelapse_session
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM timelapse_sessions WHERE printer_id = ? ORDER BY started_at DESC LIMIT ?",
                (printer_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row_to_timelapse_session(r) for r in rows]
        finally:
            await conn.close()

    async def list_all_sessions(self, limit: int = 50) -> List[Any]:
        from bambu_monitor.storage.models import row_to_timelapse_session
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM timelapse_sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row_to_timelapse_session(r) for r in rows]
        finally:
            await conn.close()

    async def save_pause(self, pause: Any) -> int:
        from bambu_monitor.storage.models import format_datetime
        conn = await self.db.get_connection()
        try:
            cursor = await conn.execute(
                """
                INSERT INTO timelapse_pauses (session_id, started_at, ended_at, duration_seconds)
                VALUES (?, ?, ?, ?)
                """,
                (
                    pause.session_id,
                    format_datetime(pause.started_at),
                    format_datetime(pause.ended_at),
                    pause.duration_seconds,
                ),
            )
            pause_id = cursor.lastrowid or 0
            pause.id = pause_id
            await conn.commit()
            return pause_id
        finally:
            await conn.close()

    async def update_pause(self, pause: Any) -> None:
        from bambu_monitor.storage.models import format_datetime
        conn = await self.db.get_connection()
        try:
            await conn.execute(
                """
                UPDATE timelapse_pauses
                SET ended_at = ?, duration_seconds = ?
                WHERE id = ?
                """,
                (
                    format_datetime(pause.ended_at),
                    pause.duration_seconds,
                    pause.id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def get_pauses_for_session(self, session_id: str) -> List[Any]:
        from bambu_monitor.storage.models import row_to_timelapse_pause
        conn = await self.db.get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM timelapse_pauses WHERE session_id = ? ORDER BY started_at ASC",
                (session_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [row_to_timelapse_pause(r) for r in rows]
        finally:
            await conn.close()

