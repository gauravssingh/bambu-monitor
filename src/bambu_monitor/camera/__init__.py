"""RTSP Camera integration package for Bambu Monitor."""

from bambu_monitor.camera.client import (
    BaseRTSPCamera,
    CameraClient,
    CameraClientProtocol,
    GenericRTSPCamera,
    TapoRTSPCamera,
    create_camera_client,
)
from bambu_monitor.camera.config import CameraConfig
from bambu_monitor.camera.exceptions import (
    CameraCaptureError,
    CameraConfigError,
    CameraConnectionError,
    CameraError,
    CameraTimeoutError,
)
from bambu_monitor.camera.models import CameraHealth, CameraType
from bambu_monitor.camera.registry import CameraRegistry
from bambu_monitor.camera.security import sanitize_rtsp_url

__all__ = [
    "BaseRTSPCamera",
    "CameraClient",
    "CameraClientProtocol",
    "CameraConfig",
    "CameraError",
    "CameraConfigError",
    "CameraConnectionError",
    "CameraTimeoutError",
    "CameraCaptureError",
    "CameraHealth",
    "CameraRegistry",
    "CameraType",
    "GenericRTSPCamera",
    "TapoRTSPCamera",
    "create_camera_client",
    "sanitize_rtsp_url",
]
