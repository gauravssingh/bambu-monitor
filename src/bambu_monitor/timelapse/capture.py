"""FrameCaptureWorker executing non-blocking interval snapshots and outage management."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from bambu_monitor.camera import CameraClient, CameraError
from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus, utc_now
from bambu_monitor.timelapse.storage import TimelapseStorage

logger = logging.getLogger(__name__)


class FrameCaptureWorker:
    """Independent background worker for periodic frame capture with degradation handling."""

    def __init__(
        self,
        session: TimelapseSession,
        camera: CameraClient,
        storage: TimelapseStorage,
        mode: str = "interval",
        telemetry_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        on_frame_captured: Optional[Callable[[int, Path], Awaitable[None]]] = None,
        on_degraded: Optional[Callable[[str], Awaitable[None]]] = None,
        on_recovered: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.session = session
        self.camera = camera
        self.storage = storage
        self.mode = mode
        self.telemetry_provider = telemetry_provider
        self.on_frame_captured = on_frame_captured
        self.on_degraded = on_degraded
        self.on_recovered = on_recovered

        self._running: bool = False
        self._paused: bool = False
        self._task: Optional[asyncio.Task[None]] = None
        self._camera_online: bool = True
        self._outage_start: Optional[datetime] = None
        self._last_capture_mono: float = 0.0
        self._min_trigger_cooldown: float = 1.0
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        """Start the async periodic capture task."""
        if self._running:
            return
        self._running = True
        self._paused = (self.session.status == TimelapseStatus.PAUSED)
        self._task = asyncio.create_task(self._capture_loop())
        logger.info(
            "FrameCaptureWorker started for session %s (mode: %s, interval: %.1fs)",
            self.session.id,
            self.mode,
            self.session.capture_interval_seconds,
        )

    def pause(self) -> None:
        """Pause capture immediately upon printer pause event."""
        if not self._paused:
            self._paused = True
            logger.info("FrameCaptureWorker paused for session %s", self.session.id)

    def resume(self) -> None:
        """Resume capture immediately upon printer resume event."""
        if self._paused:
            self._paused = False
            logger.info("FrameCaptureWorker resumed for session %s", self.session.id)

    async def stop(self) -> None:
        """Gracefully stop capture loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        try:
            await self.camera.close()
        except Exception as exc:
            logger.debug("Error closing camera on worker stop: %s", exc)
        logger.info("FrameCaptureWorker stopped for session %s", self.session.id)

    async def trigger_capture(
        self,
        reason: str = "layer_change",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Path]:
        """Trigger an immediate capture (e.g. layer change) with debounce cooldown."""
        if not self._running or self._paused:
            return None
        now = time.monotonic()
        if (now - self._last_capture_mono) < self._min_trigger_cooldown:
            logger.debug("Debouncing trigger_capture for session %s (%s)", self.session.id, reason)
            return None
        return await self._capture_one_frame(reason=reason, metadata=metadata)

    async def _capture_one_frame(
        self,
        reason: str = "interval",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Path]:
        """Execute a single frame capture, save frame, and log metadata to frames.jsonl."""
        async with self._lock:
            if not self._running or self._paused:
                return None

            try:
                frame_bytes = await self.camera.capture()
                self._last_capture_mono = time.monotonic()

                # Camera recovered from a prior outage
                if not self._camera_online:
                    self._camera_online = True
                    outage_duration = (utc_now() - (self._outage_start or utc_now())).total_seconds()
                    self.session.camera_outage_seconds += max(0.0, outage_duration)
                    self._outage_start = None
                    self.session.transition_to(TimelapseStatus.CAPTURING)
                    logger.info("Camera recovered for timelapse session %s", self.session.id)
                    if self.on_recovered:
                        try:
                            await self.on_recovered()
                        except Exception as cb_exc:
                            logger.debug("Error in on_recovered callback: %s", cb_exc)

                # Persist frame
                sequence = self.session.frame_count + 1
                session_dir = Path(self.session.storage_dir)
                saved_path = self.storage.save_frame(session_dir, sequence, frame_bytes)
                self.session.frame_count = sequence
                self.session.updated_at = utc_now()

                # Record frame metadata sidecar (frames.jsonl)
                frame_rec: Dict[str, Any] = {
                    "frame": sequence,
                    "filename": f"{sequence:06d}.jpg",
                    "timestamp": utc_now().isoformat(),
                    "reason": reason,
                    "size_bytes": len(frame_bytes),
                }
                if self.telemetry_provider:
                    try:
                        live_telem = self.telemetry_provider()
                        if live_telem and isinstance(live_telem, dict):
                            frame_rec.update(live_telem)
                    except Exception as telem_exc:
                        logger.debug("Error querying telemetry provider for frame %d: %s", sequence, telem_exc)
                if metadata:
                    frame_rec.update(metadata)
                self.storage.append_frame_metadata(session_dir, frame_rec)

                logger.debug(
                    "Captured timelapse frame %06d for %s (reason: %s, size: %d bytes)",
                    sequence,
                    self.session.id,
                    reason,
                    len(frame_bytes),
                )

                if self.on_frame_captured:
                    try:
                        await self.on_frame_captured(sequence, saved_path)
                    except Exception as cb_exc:
                        logger.debug("Error in on_frame_captured callback: %s", cb_exc)

                return saved_path

            except asyncio.CancelledError:
                raise
            except (CameraError, Exception) as exc:
                self.session.missed_frames += 1
                if self._camera_online:
                    self._camera_online = False
                    self._outage_start = utc_now()
                    self.session.camera_outage_count += 1
                    err_msg = str(exc)
                    self.session.transition_to(TimelapseStatus.DEGRADED, error=err_msg)
                    logger.warning(
                        "Camera capture degraded for session %s: %s (missed frames: %d)",
                        self.session.id,
                        err_msg,
                        self.session.missed_frames,
                    )
                    if self.on_degraded:
                        try:
                            await self.on_degraded(err_msg)
                        except Exception as cb_exc:
                            logger.debug("Error in on_degraded callback: %s", cb_exc)
                return None

    async def _capture_loop(self) -> None:
        """Main capture loop executing non-blocking periodic snapshots with exponential backoff on outage."""
        interval = max(0.01, self.session.capture_interval_seconds)
        consecutive_failures = 0
        max_backoff = 30.0

        while self._running:
            if self._paused:
                consecutive_failures = 0
                await asyncio.sleep(min(0.2, interval))
                continue

            if self.mode == "layer":
                # In layer-only mode, capture is primarily event-driven.
                # Use a 5-minute safety heartbeat interval.
                await asyncio.sleep(1.0)
                continue

            t0 = time.monotonic()
            saved = await self._capture_one_frame(reason="interval")
            elapsed = time.monotonic() - t0

            if saved is not None:
                consecutive_failures = 0
                sleep_time = max(0.001, interval - elapsed)
            else:
                consecutive_failures += 1
                # Start exponential backoff after 2 consecutive failures to tolerate transient drops
                backoff_exp = max(0, min(4, consecutive_failures - 2))
                backoff_time = min(max_backoff, interval * (2 ** backoff_exp))
                sleep_time = max(0.001, max(interval, backoff_time) - elapsed)
                if consecutive_failures > 2:
                    logger.debug(
                        "Camera degraded for %s: backoff sleep %.2fs (consecutive failures: %d)",
                        self.session.id,
                        sleep_time,
                        consecutive_failures,
                    )

            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break


