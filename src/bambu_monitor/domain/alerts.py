"""Domain models for Alerts and Alert Lifecycle state machine."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AlertStatus(str, Enum):
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(BaseModel):
    id: str
    printer_id: str
    alert_type: str
    severity: AlertSeverity = AlertSeverity.WARNING
    status: AlertStatus = AlertStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    details: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        printer_id: str,
        alert_type: str,
        severity: AlertSeverity = AlertSeverity.WARNING,
        details: Optional[Dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
    ) -> Alert:
        now = created_at or utc_now()
        alert_id = f"alt_{printer_id}_{alert_type.replace('.', '_')}_{int(now.timestamp())}"
        return cls(
            id=alert_id,
            printer_id=printer_id,
            alert_type=alert_type,
            severity=severity,
            status=AlertStatus.ACTIVE,
            created_at=now,
            details=details or {},
        )

    def acknowledge(self, at: Optional[datetime] = None) -> None:
        if self.status == AlertStatus.ACTIVE:
            self.status = AlertStatus.ACKNOWLEDGED
            self.acknowledged_at = at or utc_now()

    def resolve(self, at: Optional[datetime] = None) -> None:
        if self.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED):
            self.status = AlertStatus.RESOLVED
            self.resolved_at = at or utc_now()

    @property
    def is_active(self) -> bool:
        return self.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED)
