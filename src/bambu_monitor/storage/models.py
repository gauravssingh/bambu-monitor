"""Storage models, column constants, and serialization helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bambu_monitor.domain.alerts import Alert, AlertSeverity, AlertStatus
from bambu_monitor.domain.events import (
    DomainEvent,
    EventSeverity,
    OutboxMessage,
    OutboxStatus,
)
from bambu_monitor.domain.printer import CurrentPrinterState, Printer, PrinterState
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
