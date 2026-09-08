"""Domain models for Timelapse sessions, pauses, manifests, and state tracking."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimelapseStatus(str, Enum):
    IDLE = "idle"
    CAPTURING = "capturing"
    PAUSED = "paused"
    DEGRADED = "degraded"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


class TimelapsePause(BaseModel):
    """Record of an individual print pause interval during a timelapse session."""
    id: Optional[int] = None
    session_id: str
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: Optional[datetime] = None
    duration_seconds: float = 0.0

    def close(self, ended_at: Optional[datetime] = None) -> float:
        """Mark pause as resumed and calculate duration."""
        end_ts = ended_at or utc_now()
        self.ended_at = end_ts
        self.duration_seconds = max(0.0, (end_ts - self.started_at).total_seconds())
        return self.duration_seconds


class TimelapseCameraInfo(BaseModel):
    """Metadata describing the camera associated with a timelapse session."""
    type: str = "tapo_rtsp"
    stream: str = "stream1"
    url: Optional[str] = None


class TimelapseManifest(BaseModel):
    """Safe on-disk manifest serialized alongside frames and output video."""
    session_id: str
    print_job_id: str
    printer_id: str
    camera: Dict[str, Any]
    started_at: str
    completed_at: Optional[str] = None
    capture_interval_seconds: float = 5.0
    video_fps: int = 30
    frame_count: int = 0
    missed_frames: int = 0
    paused_seconds: float = 0.0
    status: str = TimelapseStatus.CAPTURING.value
    video_path: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


def generate_session_id(printer_id: str, print_job_id: str, started_at: datetime) -> str:
    """Deterministic, filesystem-safe timelapse session ID."""
    clean_printer = re.sub(r"[^\w]+", "_", printer_id).strip("_")
    clean_job = re.sub(r"[^\w]+", "_", print_job_id).strip("_")
    epoch = int(started_at.timestamp())
    return f"tl_{clean_printer}_{clean_job}_{epoch}"


class TimelapseSession(BaseModel):
    """First-class entity tracking an automated print timelapse."""
    id: str
    print_job_id: str
    printer_id: str
    camera_id: str = "default"
    camera_type: str = "tapo_rtsp"
    status: TimelapseStatus = TimelapseStatus.IDLE
    started_at: datetime = Field(default_factory=utc_now)
    paused_at: Optional[datetime] = None
    resumed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    frame_count: int = 0
    missed_frames: int = 0
    capture_interval_seconds: float = 5.0
    video_fps: int = 30
    video_path: Optional[str] = None
    storage_dir: str = ""
    error: Optional[str] = None
    paused_seconds: float = 0.0
    camera_outage_count: int = 0
    camera_outage_seconds: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        printer_id: str,
        print_job_id: str,
        camera_type: str = "tapo_rtsp",
        capture_interval_seconds: float = 5.0,
        video_fps: int = 30,
        storage_dir: str = "",
        started_at: Optional[datetime] = None,
        camera_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TimelapseSession:
        start_ts = started_at or utc_now()
        session_id = generate_session_id(printer_id, print_job_id, start_ts)
        return cls(
            id=session_id,
            print_job_id=print_job_id,
            printer_id=printer_id,
            camera_id=camera_id,
            camera_type=camera_type,
            status=TimelapseStatus.CAPTURING,
            started_at=start_ts,
            capture_interval_seconds=capture_interval_seconds,
            video_fps=video_fps,
            storage_dir=storage_dir,
            metadata=metadata or {},
            created_at=start_ts,
            updated_at=start_ts,
        )

    def transition_to(self, new_status: TimelapseStatus, error: Optional[str] = None, at: Optional[datetime] = None) -> None:
        """State transition helper enforcing timestamps and error tracking."""
        now = at or utc_now()
        self.status = new_status
        self.updated_at = now
        if error:
            self.error = error
        if new_status == TimelapseStatus.PAUSED:
            self.paused_at = now
        elif new_status == TimelapseStatus.CAPTURING and self.paused_at:
            self.resumed_at = now
        elif new_status in (TimelapseStatus.COMPLETED, TimelapseStatus.FAILED):
            self.completed_at = now

    def to_manifest(self) -> TimelapseManifest:
        """Build on-disk manifest representation."""
        cam_stream = self.metadata.get("camera_stream", "stream1")
        return TimelapseManifest(
            session_id=self.id,
            print_job_id=self.print_job_id,
            printer_id=self.printer_id,
            camera={
                "type": self.camera_type,
                "stream": cam_stream,
            },
            started_at=self.started_at.isoformat(),
            completed_at=self.completed_at.isoformat() if self.completed_at else None,
            capture_interval_seconds=self.capture_interval_seconds,
            video_fps=self.video_fps,
            frame_count=self.frame_count,
            missed_frames=self.missed_frames,
            paused_seconds=round(self.paused_seconds, 1),
            status=self.status.value,
            video_path=self.video_path,
            error=self.error,
            metadata=self.metadata,
        )
