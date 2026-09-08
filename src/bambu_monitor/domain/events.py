"""Domain models for Semantic Events and Outbox Messages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DomainEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    source: str
    event_type: str
    severity: EventSeverity = EventSeverity.INFO
    timestamp: datetime = Field(default_factory=utc_now)
    printer_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        printer_id: str,
        event_type: str,
        severity: EventSeverity,
        payload: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> DomainEvent:
        return cls(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            source=printer_id,
            event_type=event_type,
            severity=severity,
            timestamp=timestamp or utc_now(),
            printer_id=printer_id,
            payload=payload or {},
        )


class OutboxStatus(str, Enum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"


class OutboxMessage(BaseModel):
    id: Optional[int] = None
    event_id: str
    printer_id: str
    destination: str
    payload: Dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    last_attempt_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    error_message: Optional[str] = None
