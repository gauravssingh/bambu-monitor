"""Storage models, column constants, and serialization helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from bambu_monitor.domain.alerts import Alert, AlertSeverity, AlertStatus
from bambu_monitor.domain.events import (
    DomainEvent,
    EventSeverity,
    OutboxMessage,
    OutboxStatus,
)
from bambu_monitor.domain.printer import Printer
from bambu_monitor.domain.print_job import JobStatus, PrintJob


def parse_datetime(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def row_to_printer(row: Any) -> Printer:
    return Printer(
        id=row["id"],
        model=row["model"],
        serial_number=row["serial_number"],
        host=row["host"],
        online=bool(row["online"]),
        last_seen=parse_datetime(row["last_seen"]),
        updated_at=parse_datetime(row["updated_at"]) or datetime.now(timezone.utc),
    )


def row_to_print_job(row: Any) -> PrintJob:
    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    return PrintJob(
        id=row["id"],
        printer_id=row["printer_id"],
        filename=row["filename"],
        status=JobStatus(row["status"]),
        started_at=parse_datetime(row["started_at"]) or datetime.now(timezone.utc),
        completed_at=parse_datetime(row["completed_at"]),
        duration_seconds=int(row["duration_seconds"]),
        progress=int(row["progress"]),
        total_layers=int(row["total_layers"]),
        metadata=meta,
    )


def row_to_alert(row: Any) -> Alert:
    details = json.loads(row["details_json"]) if row["details_json"] else {}
    return Alert(
        id=row["id"],
        printer_id=row["printer_id"],
        alert_type=row["alert_type"],
        severity=AlertSeverity(row["severity"]),
        status=AlertStatus(row["status"]),
        created_at=parse_datetime(row["created_at"]) or datetime.now(timezone.utc),
        acknowledged_at=parse_datetime(row["acknowledged_at"]),
        resolved_at=parse_datetime(row["resolved_at"]),
        details=details,
    )


def row_to_domain_event(row: Any) -> DomainEvent:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    return DomainEvent(
        event_id=row["event_id"],
        source=row["printer_id"],
        event_type=row["event_type"],
        severity=EventSeverity(row["severity"]),
        timestamp=parse_datetime(row["timestamp"]) or datetime.now(timezone.utc),
        printer_id=row["printer_id"],
        payload=payload,
    )


def row_to_outbox_message(row: Any) -> OutboxMessage:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    return OutboxMessage(
        id=row["id"],
        event_id=row["event_id"],
        printer_id=row["printer_id"],
        destination=row["destination"],
        payload=payload,
        status=OutboxStatus(row["status"]),
        attempts=int(row["attempts"]),
        created_at=parse_datetime(row["created_at"]) or datetime.now(timezone.utc),
        last_attempt_at=parse_datetime(row["last_attempt_at"]),
        delivered_at=parse_datetime(row["delivered_at"]),
        error_message=row["error_message"],
    )


def row_to_timelapse_session(row: Any) -> Any:
    from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    return TimelapseSession(
        id=row["id"],
        print_job_id=row["print_job_id"],
        printer_id=row["printer_id"],
        camera_id=row["camera_id"],
        camera_type=row["camera_type"],
        status=TimelapseStatus(row["status"]),
        started_at=parse_datetime(row["started_at"]) or datetime.now(timezone.utc),
        paused_at=parse_datetime(row["paused_at"]),
        resumed_at=parse_datetime(row["resumed_at"]),
        completed_at=parse_datetime(row["completed_at"]),
        frame_count=int(row["frame_count"]),
        missed_frames=int(row["missed_frames"]),
        capture_interval_seconds=float(row["capture_interval_seconds"]),
        video_fps=int(row["video_fps"]),
        video_path=row["video_path"],
        storage_dir=row["storage_dir"],
        error=row["error"],
        paused_seconds=float(row["paused_seconds"] or 0.0),
        camera_outage_count=int(row["camera_outage_count"] or 0),
        camera_outage_seconds=float(row["camera_outage_seconds"] or 0.0),
        metadata=meta,
        created_at=parse_datetime(row["created_at"]) or datetime.now(timezone.utc),
        updated_at=parse_datetime(row["updated_at"]) or datetime.now(timezone.utc),
    )


def row_to_timelapse_pause(row: Any) -> Any:
    from bambu_monitor.timelapse.models import TimelapsePause
    return TimelapsePause(
        id=row["id"],
        session_id=row["session_id"],
        started_at=parse_datetime(row["started_at"]) or datetime.now(timezone.utc),
        ended_at=parse_datetime(row["ended_at"]),
        duration_seconds=float(row["duration_seconds"] or 0.0),
    )
