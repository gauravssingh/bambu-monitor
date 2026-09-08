"""Central TimelapseManager coordinating sessions, capture workers, and video rendering."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from bambu_monitor.camera import (
    CameraClient,
    CameraConfig,
    CameraRegistry,
    create_camera_client,
)
from bambu_monitor.config import Settings
from bambu_monitor.domain.events import DomainEvent, EventSeverity
from bambu_monitor.domain.print_job import JobStatus, PrintJob
from bambu_monitor.storage.repositories import TimelapseRepository
from bambu_monitor.timelapse.capture import FrameCaptureWorker
from bambu_monitor.timelapse.models import (
    TimelapsePause,
    TimelapseSession,
    TimelapseStatus,
    utc_now,
)
from bambu_monitor.timelapse.renderer import TimelapseRenderError, TimelapseRenderer
from bambu_monitor.timelapse.storage import TimelapseStorage

logger = logging.getLogger(__name__)


class TimelapseManager:
    """Consumes canonical print events and manages resilient, restart-safe timelapses."""

    def __init__(
        self,
        settings: Settings,
        timelapse_repo: TimelapseRepository,
        storage: TimelapseStorage,
        camera_registry: Optional[CameraRegistry] = None,
        renderer: Optional[TimelapseRenderer] = None,
        emit_event_cb: Optional[Callable[[DomainEvent], Awaitable[None]]] = None,
        state_manager: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.repo = timelapse_repo
        self.storage = storage
        self.camera_registry = camera_registry or CameraRegistry()
        self.renderer = renderer or TimelapseRenderer()
        self.emit_event_cb = emit_event_cb
        self.state_manager = state_manager

        max_concurrent = getattr(self.settings.timelapse.video, "max_concurrent", 1)
        self._render_semaphore = asyncio.Semaphore(max_concurrent)
        self._active_sessions: Dict[str, TimelapseSession] = {}
        self._workers: Dict[str, FrameCaptureWorker] = {}
        self._active_pauses: Dict[str, TimelapsePause] = {}
        self._render_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    def get_active_session(self, printer_id: str) -> Optional[TimelapseSession]:
        """O(1) active in-memory session lookup."""
        return self._active_sessions.get(printer_id)

    async def _emit_timelapse_event(
        self,
        printer_id: str,
        event_type: str,
        severity: EventSeverity,
        session: TimelapseSession,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit domain event across the application event bus and SSE subscribers."""
        payload = {
            "session_id": session.id,
            "print_job_id": session.print_job_id,
            "printer_id": printer_id,
            "status": session.status.value,
            "frame_count": session.frame_count,
            "missed_frames": session.missed_frames,
            "error": session.error,
            "video_path": session.video_path,
        }
        if extra:
            payload.update(extra)

        evt = DomainEvent.create(
            printer_id=printer_id,
            event_type=event_type,
            severity=severity,
            payload=payload,
        )
        if self.emit_event_cb:
            try:
                await self.emit_event_cb(evt)
            except Exception as exc:
                logger.debug("Error delivering timelapse domain event: %s", exc)

    def _resolve_camera_client(self, printer_id: str) -> Optional[CameraClient]:
        """Lookup registered camera or instantiate client from effective configuration."""
        client = self.camera_registry.get(printer_id)
        if client:
            return client

        cfg = self.settings.get_timelapse_config(printer_id)
        if not cfg.enabled or not cfg.camera.url:
            return None

        cam_config = CameraConfig(
            enabled=True,
            type=cfg.camera.type,
            stream=cfg.camera.stream,
            rtsp_url=cfg.camera.url,
            timeout_seconds=5.0,
        )
        client = create_camera_client(config=cam_config, printer_id=printer_id)
        self.camera_registry.register(printer_id, client)
        return client

    async def handle_domain_event(self, event: DomainEvent) -> None:
        """Central event dispatcher consuming canonical print lifecycle events."""
        event_type = event.event_type
        printer_id = event.printer_id
        payload = event.payload

        if event_type == "print.started":
            job_id = payload.get("job_id", "")
            await self.on_print_started(printer_id, job_id, payload)

        elif event_type == "print.paused":
            await self.on_print_paused(printer_id, payload)

        elif event_type == "print.resumed":
            await self.on_print_resumed(printer_id, payload)

        elif event_type == "print.layer_changed":
            await self.on_print_layer_changed(printer_id, payload)

        elif event_type == "print.completed":
            await self.on_print_completed(printer_id, payload)

        elif event_type == "print.failed":
            await self.on_print_failed(printer_id, payload)

    async def on_print_layer_changed(
        self,
        printer_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Trigger layer-based frame capture if layer or hybrid mode is active."""
        worker = self._workers.get(printer_id)
        if not worker or not worker.is_running or worker.is_paused:
            return
        cfg = self.settings.get_timelapse_config(printer_id)
        if cfg.capture.mode in ("layer", "hybrid"):
            layer = (payload or {}).get("layer")
            progress = (payload or {}).get("progress")
            await worker.trigger_capture(
                reason="layer_change",
                metadata={"layer": layer, "progress": progress},
            )

    async def on_print_started(
        self,
        printer_id: str,
        job_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[TimelapseSession]:
        """Initialize session, create directories, and spawn FrameCaptureWorker."""
        async with self._lock:
            cfg = self.settings.get_timelapse_config(printer_id)
            if not cfg.enabled:
                logger.debug("Timelapse disabled for printer '%s'", printer_id)
                return None

            camera = self._resolve_camera_client(printer_id)
            if not camera:
                logger.warning("No camera configured for printer '%s'; timelapse will not start.", printer_id)
                return None

            # Idempotency check: don't create duplicate sessions for the same job
            existing_active = self._active_sessions.get(printer_id)
            if existing_active and existing_active.print_job_id == job_id:
                logger.info("Session %s already active for job %s", existing_active.id, job_id)
                return existing_active

            # Also check DB to avoid duplicating session after restart
            db_session = await self.repo.get_session_by_job(job_id)
            if db_session:
                self._active_sessions[printer_id] = db_session
                await self._start_worker_for_session(printer_id, db_session, camera)
                return db_session

            started_at = utc_now()
            session = TimelapseSession.create(
                printer_id=printer_id,
                print_job_id=job_id,
                camera_type=cfg.camera.type,
                capture_interval_seconds=cfg.capture.interval_seconds,
                video_fps=cfg.video.fps,
                storage_dir=str(self.storage.resolve_session_dir(printer_id, f"tmp_{job_id}", started_at)),
                started_at=started_at,
                camera_id=printer_id,
                metadata={
                    "camera_stream": cfg.camera.stream,
                    "filename": (payload or {}).get("filename", ""),
                },
            )

            # Resolve actual session directory with true session id
            actual_dir = self.storage.resolve_session_dir(printer_id, session.id, started_at)
            session.storage_dir = str(actual_dir)
            self.storage.ensure_session_dirs(actual_dir)

            # Persist
            await self.repo.save_session(session)
            self.storage.save_manifest(session)

            self._active_sessions[printer_id] = session
            await self._start_worker_for_session(printer_id, session, camera)

            await self._emit_timelapse_event(printer_id, "timelapse.started", EventSeverity.INFO, session)
            logger.info("Timelapse session %s started for job %s", session.id, job_id)
            return session

    async def _start_worker_for_session(
        self,
        printer_id: str,
        session: TimelapseSession,
        camera: CameraClient,
    ) -> None:
        """Instantiate and start FrameCaptureWorker with callbacks."""
        existing_worker = self._workers.get(printer_id)
        if existing_worker:
            await existing_worker.stop()

        async def on_frame(seq: int, path: Path) -> None:
            # Throttled DB / manifest update every 10 frames
            if seq % 10 == 0:
                await self.repo.save_session(session)
                self.storage.save_manifest(session)

        async def on_degraded(err: str) -> None:
            await self.repo.save_session(session)
            self.storage.save_manifest(session)
            await self._emit_timelapse_event(printer_id, "timelapse.degraded", EventSeverity.WARNING, session)

        async def on_recovered() -> None:
            await self.repo.save_session(session)
            self.storage.save_manifest(session)
            await self._emit_timelapse_event(printer_id, "timelapse.resumed", EventSeverity.INFO, session)

        def get_telemetry_snapshot() -> Optional[Dict[str, Any]]:
            if not self.state_manager:
                return None
            st = self.state_manager.get_state(printer_id)
            if not st:
                return None
            data: Dict[str, Any] = {
                "nozzle_temp": round(st.temperatures.nozzle, 1),
                "nozzle_target": round(st.temperatures.nozzle_target, 1),
                "bed_temp": round(st.temperatures.bed, 1),
                "bed_target": round(st.temperatures.bed_target, 1),
                "printer_state": st.state.value if hasattr(st.state, "value") else str(st.state),
            }
            if st.temperatures.chamber is not None:
                data["chamber_temp"] = round(st.temperatures.chamber, 1)
            if st.print:
                data["layer"] = st.print.layer
                data["total_layers"] = st.print.total_layers
                data["progress"] = st.print.progress
                data["remaining_seconds"] = st.print.remaining_seconds
            if hasattr(st, "speed_magnitude") and st.speed_magnitude is not None:
                data["speed_percent"] = st.speed_magnitude
            elif hasattr(st, "speed_level") and st.speed_level is not None:
                data["speed_level"] = st.speed_level
            if hasattr(st, "cooling_fan_speed") and st.cooling_fan_speed is not None:
                data["cooling_fan_speed"] = st.cooling_fan_speed
            return data

        cfg = self.settings.get_timelapse_config(printer_id)
        worker = FrameCaptureWorker(
            session=session,
            camera=camera,
            storage=self.storage,
            mode=cfg.capture.mode,
            telemetry_provider=get_telemetry_snapshot,
            on_frame_captured=on_frame,
            on_degraded=on_degraded,
            on_recovered=on_recovered,
        )
        self._workers[printer_id] = worker
        worker.start()

    async def on_print_paused(self, printer_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Pause frame capture and record pause duration."""
        async with self._lock:
            session = self._active_sessions.get(printer_id)
            if not session:
                return

            worker = self._workers.get(printer_id)
            if worker:
                worker.pause()

            pause = TimelapsePause(session_id=session.id, started_at=utc_now())
            await self.repo.save_pause(pause)
            self._active_pauses[printer_id] = pause

            session.transition_to(TimelapseStatus.PAUSED)
            await self.repo.save_session(session)
            self.storage.save_manifest(session)

            await self._emit_timelapse_event(printer_id, "timelapse.paused", EventSeverity.WARNING, session)
            logger.info("Timelapse session %s paused", session.id)

    async def on_print_resumed(self, printer_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Resume frame capture and close pause record."""
        async with self._lock:
            session = self._active_sessions.get(printer_id)
            if not session:
                return

            pause = self._active_pauses.pop(printer_id, None)
            if pause:
                pause.close()
                session.paused_seconds += pause.duration_seconds
                await self.repo.update_pause(pause)

            session.transition_to(TimelapseStatus.CAPTURING)
            await self.repo.save_session(session)
            self.storage.save_manifest(session)

            worker = self._workers.get(printer_id)
            if worker:
                worker.resume()

            await self._emit_timelapse_event(printer_id, "timelapse.resumed", EventSeverity.INFO, session)
            logger.info("Timelapse session %s resumed", session.id)

    async def on_print_completed(self, printer_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Stop capture worker and queue asynchronous MP4 video generation."""
        async with self._lock:
            session = self._active_sessions.pop(printer_id, None)
            worker = self._workers.pop(printer_id, None)
            self._active_pauses.pop(printer_id, None)

            if worker:
                await worker.stop()

            if not session:
                return

            logger.info("Print completed for %s; compiling timelapse %s", printer_id, session.id)
            task = asyncio.create_task(self._finalize_and_render(printer_id, session, is_failed=False))
            self._render_tasks[session.id] = task

    async def on_print_failed(self, printer_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Stop capture worker and compile video if sufficient frames exist."""
        async with self._lock:
            session = self._active_sessions.pop(printer_id, None)
            worker = self._workers.pop(printer_id, None)
            self._active_pauses.pop(printer_id, None)

            if worker:
                await worker.stop()

            if not session:
                return

            logger.info("Print failed for %s; finalizing timelapse %s", printer_id, session.id)
            task = asyncio.create_task(self._finalize_and_render(printer_id, session, is_failed=True))
            self._render_tasks[session.id] = task

    async def _finalize_and_render(
        self,
        printer_id: str,
        session: TimelapseSession,
        is_failed: bool = False,
    ) -> None:
        """Background asynchronous rendering and cleanup."""
        cfg = self.settings.get_timelapse_config(printer_id)
        session_dir = Path(session.storage_dir)

        try:
            frames = self.storage.list_frames(session_dir)
            if not frames:
                err_msg = "No frames captured during print"
                session.transition_to(TimelapseStatus.FAILED, error=err_msg)
                await self.repo.save_session(session)
                self.storage.save_manifest(session)
                await self._emit_timelapse_event(printer_id, "timelapse.failed", EventSeverity.WARNING, session)
                return

            # Render video with bounded concurrency
            async with self._render_semaphore:
                burn_overlay = getattr(cfg.overlay, "enabled", False) if hasattr(cfg, "overlay") else False
                video_path = await self.renderer.render(
                    session=session,
                    storage=self.storage,
                    fps=cfg.video.fps,
                    codec=cfg.video.codec,
                    quality=cfg.video.quality,
                    pixel_format=cfg.video.pixel_format,
                    burn_overlay=burn_overlay,
                )

            if is_failed:
                # Video rendered for failed print; mark session as FAILED per lifecycle
                session.transition_to(TimelapseStatus.FAILED, error="Print job failed")
                await self.repo.save_session(session)
                self.storage.save_manifest(session)
                await self._emit_timelapse_event(printer_id, "timelapse.failed", EventSeverity.WARNING, session)
            else:
                # Successful print: check retention policy
                session.transition_to(TimelapseStatus.COMPLETED)
                await self.repo.save_session(session)
                self.storage.save_manifest(session)

                if cfg.retention.successful_frames == "delete_after_video":
                    deleted = self.storage.cleanup_successful_frames(session_dir)
                    logger.info("Cleaned up %d frame images after video render for %s", deleted, session.id)

                await self._emit_timelapse_event(printer_id, "timelapse.completed", EventSeverity.INFO, session)

        except TimelapseRenderError as err:
            logger.error("Timelapse render error for %s: %s", session.id, err)
            session.transition_to(TimelapseStatus.FAILED, error=str(err))
            await self.repo.save_session(session)
            self.storage.save_manifest(session)
            await self._emit_timelapse_event(printer_id, "timelapse.failed", EventSeverity.WARNING, session)
        except Exception as exc:
            logger.exception("Unexpected error finalizing timelapse %s: %s", session.id, exc)
            session.transition_to(TimelapseStatus.FAILED, error=str(exc))
            await self.repo.save_session(session)
            self.storage.save_manifest(session)
            await self._emit_timelapse_event(printer_id, "timelapse.failed", EventSeverity.CRITICAL, session)
        finally:
            self._render_tasks.pop(session.id, None)

    async def reconcile_on_startup(self, printer_id: str, active_job: Optional[PrintJob]) -> None:
        """Handle process restarts safely, reattaching to existing sessions or finalizing orphans."""
        async with self._lock:
            active_session = await self.repo.get_active_session_for_printer(printer_id)

            if active_job:
                # Check if this active job already has a session
                job_session = await self.repo.get_session_by_job(active_job.id)
                session = job_session or active_session

                if session and session.print_job_id == active_job.id:
                    logger.info(
                        "Startup reconciliation: re-attaching timelapse session %s for job %s",
                        session.id,
                        active_job.id,
                    )
                    self._active_sessions[printer_id] = session

                    # Handle state recovery
                    if session.status == TimelapseStatus.FINALIZING:
                        # Process crashed during render: retry render
                        task = asyncio.create_task(self._finalize_and_render(printer_id, session, is_failed=False))
                        self._render_tasks[session.id] = task
                        return

                    camera = self._resolve_camera_client(printer_id)
                    if camera:
                        await self._start_worker_for_session(printer_id, session, camera)
                        worker = self._workers.get(printer_id)
                        if active_job.status == JobStatus.PAUSED:
                            if worker:
                                worker.pause()
                            session.transition_to(TimelapseStatus.PAUSED)
                        else:
                            session.transition_to(TimelapseStatus.CAPTURING)
                        await self.repo.save_session(session)
                        self.storage.save_manifest(session)

                elif not session:
                    # Active job exists but no timelapse session exists yet
                    logger.info(
                        "Startup reconciliation: active print %s found without session; starting timelapse",
                        active_job.id,
                    )
                    # Use on_print_started logic
                    cfg = self.settings.get_timelapse_config(printer_id)
                    if cfg.enabled:
                        camera = self._resolve_camera_client(printer_id)
                        if camera:
                            started_at = active_job.started_at
                            new_session = TimelapseSession.create(
                                printer_id=printer_id,
                                print_job_id=active_job.id,
                                camera_type=cfg.camera.type,
                                capture_interval_seconds=cfg.capture.interval_seconds,
                                video_fps=cfg.video.fps,
                                storage_dir="",
                                started_at=started_at,
                                camera_id=printer_id,
                                metadata={"camera_stream": cfg.camera.stream, "filename": active_job.filename},
                            )
                            actual_dir = self.storage.resolve_session_dir(printer_id, new_session.id, started_at)
                            new_session.storage_dir = str(actual_dir)
                            self.storage.ensure_session_dirs(actual_dir)
                            await self.repo.save_session(new_session)
                            self.storage.save_manifest(new_session)

                            self._active_sessions[printer_id] = new_session
                            await self._start_worker_for_session(printer_id, new_session, camera)
                            if active_job.status == JobStatus.PAUSED:
                                worker = self._workers.get(printer_id)
                                if worker:
                                    worker.pause()
                                new_session.transition_to(TimelapseStatus.PAUSED)
                                await self.repo.save_session(new_session)
                                self.storage.save_manifest(new_session)

            else:
                # No active print job on startup. If an active session remains in DB, finalize it!
                if active_session:
                    logger.info(
                        "Startup reconciliation: orphaned timelapse session %s found for idle printer %s; finalizing",
                        active_session.id,
                        printer_id,
                    )
                    task = asyncio.create_task(self._finalize_and_render(printer_id, active_session, is_failed=True))
                    self._render_tasks[active_session.id] = task

    async def generate_video(self, job_or_session_id: str) -> Path:
        """Manual trigger to generate video for debugging or re-renders (used by CLI generate)."""
        session = await self.repo.get_session(job_or_session_id)
        if not session:
            session = await self.repo.get_session_by_job(job_or_session_id)

        if not session:
            raise TimelapseRenderError(f"Timelapse session not found for ID '{job_or_session_id}'")

        cfg = self.settings.get_timelapse_config(session.printer_id)
        async with self._render_semaphore:
            burn_overlay = getattr(cfg.overlay, "enabled", False) if hasattr(cfg, "overlay") else False
            video_path = await self.renderer.render(
                session=session,
                storage=self.storage,
                fps=cfg.video.fps,
                codec=cfg.video.codec,
                quality=cfg.video.quality,
                pixel_format=cfg.video.pixel_format,
                burn_overlay=burn_overlay,
            )
        session.transition_to(TimelapseStatus.COMPLETED)
        await self.repo.save_session(session)
        self.storage.save_manifest(session)
        return video_path

    async def shutdown(self) -> None:
        """Gracefully stop all workers and wait for render tasks during service shutdown."""
        async with self._lock:
            for worker in self._workers.values():
                try:
                    await worker.stop()
                except Exception:
                    pass
            self._workers.clear()

        # Wait briefly for active render tasks
        tasks = list(self._render_tasks.values())
        if tasks:
            logger.info("Waiting for %d timelapse video rendering task(s) to finish...", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
