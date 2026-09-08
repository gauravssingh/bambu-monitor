"""FrameCaptureWorker executing non-blocking interval snapshots and outage management."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

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
        on_frame_captured: Optional[Callable[[int, Path], Awaitable[None]]] = None,
        on_degraded: Optional[Callable[[str], Awaitable[None]]] = None,
        on_recovered: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.session = session
        self.camera = camera
        self.storage = storage
        self.on_frame_captured = on_frame_captured
        self.on_degraded = on_degraded
        self.on_recovered = on_recovered

        self._running: bool = False
        self._paused: bool = False
        self._task: Optional[asyncio.Task[None]] = None
        self._camera_online: bool = True
        self._outage_start: Optional[datetime] = None

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
            "FrameCaptureWorker started for session %s (interval: %.1fs)",
            self.session.id,
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

    async def _capture_loop(self) -> None:
        """Main capture loop executing non-blocking periodic snapshots."""
        interval = max(0.01, self.session.capture_interval_seconds)

        while self._running:
            if self._paused:
                await asyncio.sleep(min(0.2, interval))
                continue

            t0 = time.monotonic()
            try:
                frame_bytes = await self.camera.capture()

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

                logger.debug(
                    "Captured timelapse frame %06d for %s (size: %d bytes)",
                    sequence,
                    self.session.id,
                    len(frame_bytes),
                )

                if self.on_frame_captured:
                    try:
                        await self.on_frame_captured(sequence, saved_path)
                    except Exception as cb_exc:
                        logger.debug("Error in on_frame_captured callback: %s", cb_exc)

            except asyncio.CancelledError:
                break
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

            elapsed = time.monotonic() - t0
            sleep_time = max(0.001, interval - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break
