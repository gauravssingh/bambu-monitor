"""CameraClient abstraction wrapping FFmpeg subprocess execution."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, Optional

from bambu_monitor.camera.config import CameraConfig
from bambu_monitor.camera.exceptions import (
    CameraCaptureError,
    CameraConfigError,
    CameraConnectionError,
    CameraError,
    CameraTimeoutError,
)
from bambu_monitor.camera.security import sanitize_rtsp_url

logger = logging.getLogger(__name__)

# JPEG magic header bytes
JPEG_SOI_MARKER = b"\xff\xd8"


class CameraClient:
    """Transport-agnostic camera client for capturing frames from an RTSP stream.
    
    All FFmpeg subprocess details, stream probing compromises, and socket timeouts
    are strictly encapsulated within this client.
    """

    def __init__(self, config: CameraConfig, printer_id: str = "") -> None:
        self.config = config
        self.printer_id = printer_id

    @property
    def sanitized_url(self) -> str:
        """Sanitized RTSP URL with credentials masked."""
        return self.config.sanitized_rtsp_url

    async def snapshot(self, timeout: Optional[float] = None) -> bytes:
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

        if not self.config.rtsp_url:
            raise CameraConfigError(f"RTSP URL is not configured for printer '{self.printer_id}'")

        effective_timeout = timeout if timeout is not None else self.config.timeout_seconds
        cmd = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-probesize", str(self.config.probe_size_bytes),
            "-analyzeduration", str(self.config.analyze_duration_us),
            "-i", self.config.rtsp_url,
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

    async def test_connection(self) -> Dict[str, Any]:
        """Probe the RTSP stream and measure latency, resolution, codec, and fps.
        
        Returns a diagnostic dictionary suitable for CLI output or diagnostics API.
        """
        result: Dict[str, Any] = {
            "printer_id": self.printer_id,
            "sanitized_url": self.sanitized_url,
            "connected": False,
            "error": None,
            "snapshot_latency_ms": None,
            "image_size_bytes": None,
            "codec": None,
            "resolution": None,
            "fps": None,
        }

        # 1. Test snapshot capture
        t0 = time.perf_counter()
        try:
            image_bytes = await self.snapshot()
            latency = (time.perf_counter() - t0) * 1000
            result["connected"] = True
            result["snapshot_latency_ms"] = round(latency, 1)
            result["image_size_bytes"] = len(image_bytes)
        except CameraError as exc:
            result["connected"] = False
            result["error"] = str(exc)
            return result
        except Exception as exc:
            result["connected"] = False
            result["error"] = sanitize_rtsp_url(str(exc))
            return result

        # 2. Probe stream metadata via ffprobe if available
        probe_cmd = [
            self.config.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,r_frame_rate",
            "-of", "json",
            "-rtsp_transport", "tcp",
            "-i", self.config.rtsp_url,
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
                    result["codec"] = s.get("codec_name")
                    width = s.get("width")
                    height = s.get("height")
                    if width and height:
                        result["resolution"] = f"{width}x{height}"
                    r_fps = s.get("r_frame_rate")
                    if r_fps and "/" in r_fps:
                        num, den = r_fps.split("/")
                        try:
                            if float(den) > 0:
                                result["fps"] = f"{round(float(num) / float(den), 1)} fps"
                        except ValueError:
                            result["fps"] = r_fps
        except Exception:
            # Metadata probe is optional diagnostics; failure to ffprobe does not invalidate connectivity
            pass

        return result
