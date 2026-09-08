"""Camera domain models and health definitions."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CameraType(str, Enum):
    TAPO_RTSP = "tapo_rtsp"
    GENERIC_RTSP = "generic_rtsp"
    ONVIF = "onvif"


class CameraHealth(BaseModel):
    """Health check diagnostic model for camera client probing."""
    printer_id: str = ""
    sanitized_url: str = ""
    camera_type: str = CameraType.TAPO_RTSP.value
    connected: bool = False
    error: Optional[str] = None
    latency_ms: Optional[float] = None
    image_size_bytes: Optional[int] = None
    codec: Optional[str] = None
    resolution: Optional[str] = None
    fps: Optional[str] = None
    last_checked_at: datetime = Field(default_factory=utc_now)
