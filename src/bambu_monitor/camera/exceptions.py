"""Camera domain exceptions with automatic credential sanitization."""

from __future__ import annotations

from bambu_monitor.camera.security import sanitize_rtsp_url


class CameraError(Exception):
    """Base exception for all camera-related errors.

    Guarantees that error messages have any RTSP credentials masked.
    """
    def __init__(self, message: str) -> None:
        super().__init__(sanitize_rtsp_url(str(message)))


class CameraConfigError(CameraError):
    """Raised when camera configuration is missing, disabled, or invalid."""
    pass


class CameraConnectionError(CameraError):
    """Raised when connection to the RTSP camera stream cannot be established."""
    pass


class CameraTimeoutError(CameraError):
    """Raised when camera snapshot or communication exceeds the configured timeout."""
    pass


class CameraCaptureError(CameraError):
    """Raised when FFmpeg fails to capture a valid frame."""
    pass
