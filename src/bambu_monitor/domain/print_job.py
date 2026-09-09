"""Domain models for Print Jobs and lifecycle tracking."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    PREPARE = "prepare"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to alphanumeric and underscores for deterministic job IDs."""
    base = filename.split("/")[-1]
    base = re.sub(r"\.(3mf|gcode)$", "", base, flags=re.IGNORECASE)
    sanitized = re.sub(r"[^\w]+", "_", base).strip("_")
    return sanitized or "print"


def generate_job_id(printer_id: str, filename: str, started_at: datetime) -> str:
    """Generate a unique job ID: job_{printer_id}_{sanitized_filename}_{epoch}_{uuid}.

    A uuid fragment is appended because two prints of the same file started
    within the same wall-clock second must not collide (the epoch alone is
    only second-precise).
    """
    clean_printer = re.sub(r"[^\w]+", "_", printer_id).strip("_")
    clean_file = sanitize_filename(filename)
    epoch = int(started_at.timestamp())
    return f"job_{clean_printer}_{clean_file}_{epoch}_{uuid.uuid4().hex[:8]}"


class PrintJob(BaseModel):
    id: str
    printer_id: str
    filename: str
    status: JobStatus = JobStatus.PREPARE
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    duration_seconds: int = 0
    progress: int = 0
    layer: int = 0
    total_layers: int = 0
    remaining_seconds: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        printer_id: str,
        filename: str,
        started_at: Optional[datetime] = None,
        status: JobStatus = JobStatus.PREPARE,
        progress: int = 0,
        layer: int = 0,
        total_layers: int = 0,
        remaining_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PrintJob:
        start_ts = started_at or utc_now()
        job_id = generate_job_id(printer_id, filename, start_ts)
        return cls(
            id=job_id,
            printer_id=printer_id,
            filename=filename,
            status=status,
            started_at=start_ts,
            progress=progress,
            layer=layer,
            total_layers=total_layers,
            remaining_seconds=remaining_seconds,
            metadata=metadata or {},
        )

    def update_progress(
        self,
        progress: Optional[int] = None,
        layer: Optional[int] = None,
        total_layers: Optional[int] = None,
        remaining_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> None:
        if progress is not None:
            self.progress = progress
        if layer is not None:
            self.layer = layer
        if total_layers is not None:
            self.total_layers = total_layers
        if remaining_seconds is not None:
            self.remaining_seconds = remaining_seconds

        current_time = now or utc_now()
        self.duration_seconds = max(0, int((current_time - self.started_at).total_seconds()))

    def transition_to(self, new_status: JobStatus, at: Optional[datetime] = None) -> None:
        self.status = new_status
        current_time = at or utc_now()
        self.duration_seconds = max(0, int((current_time - self.started_at).total_seconds()))
        if new_status in (JobStatus.COMPLETED, JobStatus.FAILED):
            self.completed_at = current_time
