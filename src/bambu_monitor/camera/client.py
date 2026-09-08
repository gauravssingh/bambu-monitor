"""CameraClient abstraction wrapping FFmpeg subprocess execution."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from bambu_monitor.camera.config import CameraConfig
from bambu_monitor.camera.exceptions import (
    CameraCaptureError,
    CameraConfigError,
    CameraConnectionError,
    CameraError,
    CameraTimeoutError,
)
from bambu_monitor.camera.models import CameraHealth, CameraType
from bambu_monitor.camera.security import sanitize_rtsp_url

logger = logging.getLogger(__name__)

# JPEG magic header bytes
JPEG_SOI_MARKER = b"\xff\xd8"


@runtime_checkable
class CameraClientProtocol(Protocol):
    """Vendor-neutral camera interface for stream capture and health diagnostics."""

    async def connect(self) -> None:
        """Verify/establish connection to the camera stream."""
        ...

    async def capture(self, timeout: Optional[float] = None) -> bytes:
        """Capture a single image frame as raw JPEG bytes."""
        ...

    async def health(self) -> CameraHealth:
        """Probe camera status, stream latency, and metadata."""
        ...

    async def close(self) -> None:
        """Close camera connection and release resources."""
        ...


class CameraClient:
    """Transport-agnostic camera client for capturing frames from an RTSP stream.
    
    All FFmpeg subprocess details, stream probing compromises, and socket timeouts
    are strictly encapsulated within this client.
    """

    camera_type: str = CameraType.GENERIC_RTSP.value

    def __init__(self, config: CameraConfig, printer_id: str = "") -> None:
        self.config = config
        self.printer_id = printer_id

    @property
    def sanitized_url(self) -> str:
        """Sanitized RTSP URL with credentials masked."""
        return self.config.sanitized_rtsp_url

    def _get_active_stream_url(self) -> str:
        """Return the effective RTSP URL for frame capture."""
        return self.config.rtsp_url

    async def connect(self) -> None:
        """Verify RTSP connection and stream availability."""
        if not self.config.enabled:
            raise CameraConfigError(f"Camera is disabled for printer '{self.printer_id}'")
        if not self.config.rtsp_url:
            raise CameraConfigError(f"RTSP URL is not configured for printer '{self.printer_id}'")

        try:
            await self.capture()
        except CameraError:
            raise
        except Exception as exc:
            raise CameraConnectionError(f"Failed to connect to camera stream: {sanitize_rtsp_url(str(exc))}") from exc

    async def capture(self, timeout: Optional[float] = None) -> bytes:
        """Capture a single JPEG snapshot directly from the RTSP stream into memory.
        
        Guarantees:
        1. Async subprocess non-blocking execution.
        2. Output piped directly via stdout (zero filesystem writes).
        3. Hard timeout enforcement with guaranteed subprocess termination (kill + wait).
        4. Rejection of empty or invalid output.
        5. Zero leakage of RTSP passwords in exceptions or logs.
        """
        if not self.config.enabled:
            raise CameraConfigError(f"Camera is disabled for printer '{self.printer_id}'")

        rtsp_url = self._get_active_stream_url()
        if not rtsp_url:
            raise CameraConfigError(f"RTSP URL is not configured for printer '{self.printer_id}'")

        effective_timeout = timeout if timeout is not None else self.config.timeout_seconds
        cmd = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-probesize", str(self.config.probe_size_bytes),
            "-analyzeduration", str(self.config.analyze_duration_us),
            "-i", rtsp_url,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-",
        ]

        logger.debug(
            "Capturing camera snapshot for '%s' from %s (timeout: %.1fs)",
            self.printer_id,
            self.sanitized_url,
            effective_timeout,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise CameraCaptureError(f"FFmpeg executable not found: '{self.config.ffmpeg_bin}'")
        except Exception as exc:
            raise CameraCaptureError(f"Failed to spawn FFmpeg process: {sanitize_rtsp_url(str(exc))}")

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise CameraTimeoutError(
                f"Camera snapshot for '{self.printer_id}' timed out after {effective_timeout:.1f}s"
            )
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise

        if process.returncode != 0:
            err_raw = stderr.decode(errors="replace").strip() if stderr else ""
            err_msg = sanitize_rtsp_url(err_raw) or f"FFmpeg exited with error code {process.returncode}"
            
            # Categorize connection failures
            err_lower = err_msg.lower()
            if any(term in err_lower for term in ("connection refused", "route to host", "timed out", "unauthorized", "401", "404")):
                raise CameraConnectionError(f"Failed to connect to camera stream: {err_msg}")
            raise CameraCaptureError(f"Camera frame capture failed: {err_msg}")

        if not stdout:
            raise CameraCaptureError("FFmpeg returned an empty image")

        if not stdout.startswith(JPEG_SOI_MARKER):
            raise CameraCaptureError("FFmpeg output is not a valid JPEG stream")

        return stdout

    async def snapshot(self, timeout: Optional[float] = None) -> bytes:
        """Alias for capture() for backward compatibility."""
        return await self.capture(timeout=timeout)

    async def health(self) -> CameraHealth:
        """Probe the RTSP stream and measure latency, resolution, codec, and fps."""
        health = CameraHealth(
            printer_id=self.printer_id,
            sanitized_url=self.sanitized_url,
            camera_type=self.camera_type,
            connected=False,
        )

        # 1. Test snapshot capture
        t0 = time.perf_counter()
        try:
            image_bytes = await self.capture()
            latency = (time.perf_counter() - t0) * 1000
            health.connected = True
            health.latency_ms = round(latency, 1)
            health.image_size_bytes = len(image_bytes)
        except CameraError as exc:
            health.connected = False
            health.error = str(exc)
            return health
        except Exception as exc:
            health.connected = False
            health.error = sanitize_rtsp_url(str(exc))
            return health

        # 2. Probe stream metadata via ffprobe if available
        rtsp_url = self._get_active_stream_url()
        probe_cmd = [
            self.config.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,r_frame_rate",
            "-of", "json",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
        ]

        try:
            probe_proc = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(probe_proc.communicate(), timeout=4.0)
            if probe_proc.returncode == 0 and stdout:
                data = json.loads(stdout.decode(errors="replace"))
                streams = data.get("streams", [])
                if streams:
                    s = streams[0]
                    health.codec = s.get("codec_name")
                    width = s.get("width")
                    height = s.get("height")
                    if width and height:
                        health.resolution = f"{width}x{height}"
                    r_fps = s.get("r_frame_rate")
                    if r_fps and "/" in r_fps:
                        num, den = r_fps.split("/")
                        try:
                            if float(den) > 0:
                                health.fps = f"{round(float(num) / float(den), 1)} fps"
                        except ValueError:
                            health.fps = r_fps
        except Exception:
            # Optional diagnostics; failure to ffprobe does not invalidate connectivity
            pass

        return health

    async def test_connection(self) -> Dict[str, Any]:
        """Probe stream and return dictionary format for backward compatibility."""
        h = await self.health()
        return {
            "printer_id": h.printer_id,
            "sanitized_url": h.sanitized_url,
            "connected": h.connected,
            "error": h.error,
            "snapshot_latency_ms": h.latency_ms,
            "image_size_bytes": h.image_size_bytes,
            "codec": h.codec,
            "resolution": h.resolution,
            "fps": h.fps,
        }

    async def close(self) -> None:
        """Release any active client resources."""
        pass


class BaseRTSPCamera(CameraClient):
    """Base RTSP camera client."""
    pass


class TapoRTSPCamera(BaseRTSPCamera):
    """TP-Link Tapo RTSP camera client.
    
    Supports standard Tapo stream identifiers:
    - stream1: Primary high-definition stream (1080p / 2K)
    - stream2: Substream standard-definition stream (360p)
    """
    camera_type: str = CameraType.TAPO_RTSP.value

    def _get_active_stream_url(self) -> str:
        """Resolve stream URL, respecting stream selection if substream is requested."""
        if self.config.stream == "stream2" and self.config.substream_url:
            return self.config.substream_url
        return self.config.rtsp_url


class GenericRTSPCamera(BaseRTSPCamera):
    """Generic vendor-agnostic RTSP camera client."""
    camera_type: str = CameraType.GENERIC_RTSP.value


def create_camera_client(config: CameraConfig, printer_id: str = "") -> CameraClient:
    """Factory creating appropriate CameraClient implementation based on config type."""
    cam_type = (config.type or "").lower().strip()
    if cam_type in ("tapo_rtsp", "tapo"):
        return TapoRTSPCamera(config=config, printer_id=printer_id)
    return GenericRTSPCamera(config=config, printer_id=printer_id)
