"""Configuration model for RTSP Camera monitoring."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, field_validator

from bambu_monitor.camera.security import sanitize_rtsp_url


class CameraConfig(BaseModel):
    """Configuration for an RTSP camera stream associated with a printer."""

    enabled: bool = True
    rtsp_url: str = Field(default="", description="Primary RTSP stream URL (e.g. /stream1 HD)")
    substream_url: Optional[str] = Field(
        default=None,
        description="Optional secondary RTSP stream URL (e.g. /stream2 SD)",
    )
    timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        description="Timeout in seconds for camera snapshot operations",
    )
    probe_size_bytes: int = Field(
        default=1048576,
        ge=32768,
        description="FFmpeg stream probe buffer size in bytes (default: 1MB)",
    )
    analyze_duration_us: int = Field(
        default=1000000,
        ge=100000,
        description="FFmpeg stream analyze duration in microseconds (default: 1.0s)",
    )
    ffmpeg_bin: str = Field(
        default="ffmpeg",
        description="Path or binary name for FFmpeg executable",
    )
    ffprobe_bin: str = Field(
        default="ffprobe",
        description="Path or binary name for FFprobe executable",
    )

    @field_validator("rtsp_url", mode="before")
    @classmethod
    def _coerce_url(cls, v: object) -> str:
        return "" if v is None else str(v).strip()

    @property
    def sanitized_rtsp_url(self) -> str:
        """Returns the RTSP URL with any embedded password masked."""
        return sanitize_rtsp_url(self.rtsp_url)
