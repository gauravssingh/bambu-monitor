"""Central State Manager for in-memory state, patch merging, lifecycle, and alert tracking."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

from bambu_monitor.config import Settings
from bambu_monitor.domain.alerts import Alert, AlertSeverity
from bambu_monitor.domain.events import (
    DomainEvent,
    EventSeverity,
)
from bambu_monitor.domain.printer import (
    CurrentPrinterState,
    PrinterState,
    PrintJobSnapshot,
)
from bambu_monitor.domain.print_job import JobStatus, PrintJob, sanitize_filename
from bambu_monitor.domain.telemetry import TelemetryPatch
from bambu_monitor.storage.repositories import (
    AlertRepository,
    EventRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
)

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StateManager:
    """Manages fast in-memory current state, lifecycle transitions, alerts, and event emission."""

    def __init__(
        self,
        settings: Settings,
        printer_repo: PrinterRepository,
        job_repo: JobRepository,
        alert_repo: AlertRepository,
        event_repo: EventRepository,
        outbox_repo: OutboxRepository,
    ):
        self.settings = settings
        self.printer_repo = printer_repo
        self.job_repo = job_repo
        self.alert_repo = alert_repo
        self.event_repo = event_repo
        self.outbox_repo = outbox_repo

        # In-memory fast state keyed by printer_id
        self._states: Dict[str, CurrentPrinterState] = {}
        self._active_jobs: Dict[str, PrintJob] = {}
        # Active alerts: printer_id -> alert_type -> Alert
        self._active_alerts: Dict[str, Dict[str, Alert]] = {}

        # Stall tracking: printer_id -> (progress, layer, remaining_seconds, last_change_time)
        self._last_progress_marker: Dict[str, Tuple[int, int, Optional[int], datetime]] = {}

        # SSE subscriber queues: printer_id -> set of asyncio.Queue
        self._subscribers: Dict[str, Set[asyncio.Queue[DomainEvent]]] = {}
        # Per-printer pipeline locks: serialize state mutation AND event emission
        # for a single printer (preserving event order) without letting one
        # printer's slow event listener (e.g. a multi-second camera capture)
        # stall telemetry for every other printer.
        self._printer_locks: Dict[str, asyncio.Lock] = {}

        # External and subsystem event listeners (e.g. TimelapseManager)
        self._event_listeners: List[Any] = []
        self._reconcile_listeners: List[Any] = []

    def add_event_listener(self, listener: Any) -> None:
        """Register an async callback invoked on every domain event emission."""
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def add_reconcile_listener(self, listener: Any) -> None:
        """Register an async callback invoked on startup reconciliation."""
        if listener not in self._reconcile_listeners:
            self._reconcile_listeners.append(listener)

    def register_printer(self, printer_id: str, model: str = "A1") -> None:
        """Initialize in-memory state container for a printer."""
        if printer_id not in self._states:
            self._states[printer_id] = CurrentPrinterState(
                printer_id=printer_id,
                model=model,
                online=False,
                state=PrinterState.UNKNOWN,
            )
            self._active_alerts[printer_id] = {}
            self._subscribers[printer_id] = set()
            self._printer_locks.setdefault(printer_id, asyncio.Lock())

    def _printer_lock(self, printer_id: str) -> asyncio.Lock:
        """Return the pipeline lock for one printer, creating it on demand."""
        return self._printer_locks.setdefault(printer_id, asyncio.Lock())

    def get_state(self, printer_id: str) -> Optional[CurrentPrinterState]:
        """O(1) in-memory state lookup."""
        return self._states.get(printer_id)

    def get_active_job(self, printer_id: str) -> Optional[PrintJob]:
        """O(1) active print job lookup."""
        return self._active_jobs.get(printer_id)

    def get_active_alerts(self, printer_id: str) -> List[Alert]:
        """O(1) active alerts lookup."""
        alerts_map = self._active_alerts.get(printer_id, {})
        return list(alerts_map.values())

    async def subscribe_events(
        self, printer_id: str, heartbeat_seconds: float = 15.0
    ) -> AsyncGenerator[Optional[DomainEvent], None]:
        """Subscribe to live domain events via an asyncio.Queue for SSE.

        Yields ``None`` every ``heartbeat_seconds`` of silence so the caller
        can send a keep-alive and re-check for a client disconnect; otherwise
        an idle connection with no events would only be noticed once traffic
        eventually flows (or never, if it doesn't).
        """
        if printer_id not in self._subscribers:
            self._subscribers[printer_id] = set()

        queue: asyncio.Queue[DomainEvent] = asyncio.Queue(maxsize=256)
        self._subscribers[printer_id].add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except asyncio.TimeoutError:
                    yield None
                    continue
                yield event
        finally:
            self._subscribers[printer_id].discard(queue)

    async def _emit_event(self, event: DomainEvent) -> None:
        """Persist event, enqueue to outbox, and broadcast to SSE subscribers."""
        if event.event_type in {
            "filament.runout",
            "filament.runout_cleared",
            "print.possible_blockage",
            "print.failed",
            "print.paused",
        }:
            printer_cfg = next((p for p in self.settings.printers if p.id == event.printer_id), None)
            if printer_cfg and printer_cfg.camera and printer_cfg.camera.enabled and printer_cfg.camera.rtsp_url:
                base_url = self.settings.application.public_base_url.rstrip("/")
                event.payload.setdefault(
                    "camera_snapshot_url",
                    f"{base_url}/api/v1/printers/{event.printer_id}/camera/snapshot",
                )

        if event.event_type == "timelapse.completed":
            base_url = self.settings.application.public_base_url.rstrip("/")
            session_id = event.payload.get("session_id")
            if session_id:
                event.payload.setdefault(
                    "timelapse_video_url",
                    f"{base_url}/api/v1/printers/{event.printer_id}/timelapses/{session_id}/video",
                )

        # 1. Persist event and enqueue delivery in one SQLite transaction.
        destination = self.settings.events.delivery.endpoint
        await self.event_repo.save_and_enqueue(event, destination)

        # 2. Broadcast to SSE subscribers (bounded queues; drop oldest for
        # slow consumers rather than growing memory without limit)
        queues = self._subscribers.get(event.printer_id, set())
        for q in list(queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except Exception:
                    logger.warning("Dropped SSE event for slow subscriber of %s", event.printer_id)
            except Exception as e:
                logger.debug("Failed to put event into SSE queue: %s", e)

        # 3. Notify internal registered subsystem listeners (e.g. TimelapseManager)
        for listener in list(self._event_listeners):
            try:
                await listener(event)
            except Exception as exc:
                logger.debug("Error in event listener for %s: %s", event.event_type, exc)

    @staticmethod
    def _job_identity_changed(job: PrintJob, patch: TelemetryPatch) -> bool:
        """Return true when a printer report identifies a different print."""
        metadata = job.metadata
        for field in ("subtask_id", "task_id"):
            incoming = getattr(patch, field)
            existing = metadata.get(field)
            if incoming and existing and incoming != existing:
                return True
        if patch.subtask_name and sanitize_filename(patch.subtask_name) != sanitize_filename(job.filename):
            return True
        return False

    @staticmethod
    def _is_hard_identity_change(job: PrintJob, patch: TelemetryPatch) -> bool:
        """True only for task/subtask-ID mismatches — unambiguous evidence of a
        different print. A name-only mismatch is not: printers frequently send
        the filename on a later report than the first progress delta."""
        metadata = job.metadata
        for field in ("subtask_id", "task_id"):
            incoming = getattr(patch, field)
            existing = metadata.get(field)
            if incoming and existing and incoming != existing:
                return True
        return False

    async def _supersede_job(self, job: PrintJob, patch: TelemetryPatch) -> None:
        """Close stale persisted state without emitting a false failure alert."""
        job.transition_to(JobStatus.FAILED, patch.timestamp)
        job.metadata["superseded_by"] = {
            "subtask_id": patch.subtask_id,
            "task_id": patch.task_id,
            "filename": patch.subtask_name,
        }
        await self.job_repo.save(job)

    async def apply_patch(self, patch: TelemetryPatch) -> List[DomainEvent]:
        """Core pipeline: Merge patch into in-memory state, evaluate lifecycles, and emit events.

        The lock is scoped per printer: mutation and emission for printer X are
        strictly ordered, but printer Y's pipeline runs concurrently.
        """
        async with self._printer_lock(patch.printer_id):
            printer_id = patch.printer_id
            if printer_id not in self._states:
                self.register_printer(printer_id)

            state = self._states[printer_id]
            generated_events: List[DomainEvent] = []

            # 1. Connection status transitions
            if patch.online is not None and patch.online != state.online:
                state.online = patch.online
                event_type = "printer.online" if patch.online else "printer.offline"
                severity = EventSeverity.INFO if patch.online else EventSeverity.WARNING
                evt = DomainEvent.create(printer_id, event_type, severity, {"online": patch.online}, patch.timestamp)
                generated_events.append(evt)
                try:
                    await self.printer_repo.update_current_state(printer_id, state)
                except Exception as exc:
                    logger.debug("Failed updating immediate current state on connection change: %s", exc)

            # 2. Temperature patch merge (preserving untouched fields)
            if patch.nozzle_temperature is not None:
                state.temperatures.nozzle = patch.nozzle_temperature
            if patch.nozzle_target_temperature is not None:
                state.temperatures.nozzle_target = patch.nozzle_target_temperature
            if patch.bed_temperature is not None:
                state.temperatures.bed = patch.bed_temperature
            if patch.bed_target_temperature is not None:
                state.temperatures.bed_target = patch.bed_target_temperature
            if patch.chamber_temperature is not None:
                state.temperatures.chamber = patch.chamber_temperature
            if patch.speed_magnitude is not None:
                state.speed_magnitude = patch.speed_magnitude
            if patch.cooling_fan_speed is not None:
                state.cooling_fan_speed = patch.cooling_fan_speed
            prev_speed_level = state.speed_level
            if patch.speed_level is not None:
                state.speed_level = patch.speed_level

            # 3. Last seen
            state.last_seen = patch.timestamp
            state.updated_at = patch.timestamp

            # 4. Printer state updates
            target_state = patch.state
            if target_state is None and patch.gcode_state:
                from bambu_monitor.domain.telemetry import map_gcode_state_to_printer_state
                target_state = map_gcode_state_to_printer_state(patch.gcode_state)

            if target_state is not None:
                state.state = target_state

            # 5. Print Job Lifecycle Tracking
            active_job = self._active_jobs.get(printer_id)

            if active_job and self._job_identity_changed(active_job, patch):
                if self._is_hard_identity_change(active_job, patch):
                    await self._supersede_job(active_job, patch)
                    self._active_jobs.pop(printer_id, None)
                    self._last_progress_marker.pop(printer_id, None)
                    active_job = None
                else:
                    # Name-only change (subtask_name arriving on a later
                    # report than the first progress delta): rename the job in
                    # place instead of recording a phantom FAILED job and a
                    # duplicate of the same physical print.
                    active_job.filename = patch.subtask_name
                    await self.job_repo.save(active_job)

            # Check if a new print job should start
            if active_job is None:
                is_starting = state.state in (PrinterState.PREPARING, PrinterState.PRINTING)
                if is_starting:
                    filename = patch.subtask_name or "print"
                    job_status = JobStatus.PREPARE if state.state == PrinterState.PREPARING else JobStatus.RUNNING
                    active_job = PrintJob.create(
                        printer_id=printer_id,
                        filename=filename,
                        started_at=patch.timestamp,
                        status=job_status,
                        progress=patch.progress or 0,
                        layer=patch.layer or 0,
                        total_layers=patch.total_layers or 0,
                        remaining_seconds=patch.remaining_seconds,
                        metadata={
                            "task_id": patch.task_id,
                            "subtask_id": patch.subtask_id,
                        },
                    )
                    self._active_jobs[printer_id] = active_job
                    await self.job_repo.save(active_job)

                    # Reset stall tracking
                    self._last_progress_marker[printer_id] = (
                        active_job.progress,
                        active_job.layer,
                        active_job.remaining_seconds,
                        patch.timestamp,
                    )

                    evt = DomainEvent.create(
                        printer_id=printer_id,
                        event_type="print.started",
                        severity=EventSeverity.INFO,
                        payload={
                            "job_id": active_job.id,
                            "filename": active_job.filename,
                            "started_at": active_job.started_at.isoformat(),
                            "total_layers": active_job.total_layers,
                        },
                        timestamp=patch.timestamp,
                    )
                    generated_events.append(evt)
            else:
                # Existing job: update progress fields if provided
                prev_prog = active_job.progress
                prev_layer = active_job.layer
                prev_rem = active_job.remaining_seconds

                active_job.update_progress(
                    progress=patch.progress,
                    layer=patch.layer,
                    total_layers=patch.total_layers,
                    remaining_seconds=patch.remaining_seconds,
                    now=patch.timestamp,
                )

                # Track if meaningful progress advanced
                progress_advanced = (
                    active_job.progress > prev_prog
                    or active_job.layer > prev_layer
                    or (prev_rem is not None and active_job.remaining_seconds is not None and active_job.remaining_seconds < prev_rem)
                )

                if progress_advanced or printer_id not in self._last_progress_marker:
                    self._last_progress_marker[printer_id] = (
                        active_job.progress,
                        active_job.layer,
                        active_job.remaining_seconds,
                        patch.timestamp,
                    )
                    # If an active stall alert exists, RESOLVE it!
                    alerts_map = self._active_alerts.get(printer_id, {})
                    if "print.possible_blockage" in alerts_map:
                        stall_alert = alerts_map.pop("print.possible_blockage")
                        stall_alert.resolve(patch.timestamp)
                        await self.alert_repo.save(stall_alert)
                        cleared_evt = DomainEvent.create(
                            printer_id=printer_id,
                            event_type="print.blockage_cleared",
                            severity=EventSeverity.INFO,
                            payload={
                                "job_id": active_job.id,
                                "progress": active_job.progress,
                                "layer": active_job.layer,
                            },
                            timestamp=patch.timestamp,
                        )
                        generated_events.append(cleared_evt)

                # Emit print.layer_changed event when layer advances
                if active_job.layer > prev_layer:
                    layer_evt = DomainEvent.create(
                        printer_id=printer_id,
                        event_type="print.layer_changed",
                        severity=EventSeverity.INFO,
                        payload={
                            "job_id": active_job.id,
                            "layer": active_job.layer,
                            "prev_layer": prev_layer,
                            "total_layers": active_job.total_layers,
                            "progress": active_job.progress,
                        },
                        timestamp=patch.timestamp,
                    )
                    generated_events.append(layer_evt)

                # Emit print.speed_changed event when speed level changes
                if (
                    patch.speed_level is not None
                    and prev_speed_level is not None
                    and patch.speed_level != prev_speed_level
                ):
                    speed_evt = DomainEvent.create(
                        printer_id=printer_id,
                        event_type="print.speed_changed",
                        severity=EventSeverity.INFO,
                        payload={
                            "job_id": active_job.id,
                            "speed_level": patch.speed_level,
                            "prev_speed_level": prev_speed_level,
                            "speed_magnitude": patch.speed_magnitude or state.speed_magnitude,
                        },
                        timestamp=patch.timestamp,
                    )
                    generated_events.append(speed_evt)

                # State transitions for active job
                if state.state == PrinterState.PRINTING and active_job.status == JobStatus.PREPARE:
                    active_job.transition_to(JobStatus.RUNNING, patch.timestamp)
                    await self.job_repo.save(active_job)
                    self._last_progress_marker[printer_id] = (
                        active_job.progress,
                        active_job.layer,
                        active_job.remaining_seconds,
                        patch.timestamp,
                    )

                elif state.state == PrinterState.PAUSED and active_job.status != JobStatus.PAUSED:
                    active_job.transition_to(JobStatus.PAUSED, patch.timestamp)
                    await self.job_repo.save(active_job)
                    # A confirmed runout is a more specific pause reason and
                    # must generate one notification, not pause + runout.
                    if patch.filament_runout is not True:
                        evt = DomainEvent.create(
                            printer_id=printer_id,
                            event_type="print.paused",
                            severity=EventSeverity.WARNING,
                            payload={"job_id": active_job.id, "progress": active_job.progress},
                            timestamp=patch.timestamp,
                        )
                        generated_events.append(evt)

                elif state.state == PrinterState.PRINTING and active_job.status == JobStatus.PAUSED:
                    active_job.transition_to(JobStatus.RUNNING, patch.timestamp)
                    await self.job_repo.save(active_job)
                    # Reset stall progress marker upon resuming
                    self._last_progress_marker[printer_id] = (
                        active_job.progress,
                        active_job.layer,
                        active_job.remaining_seconds,
                        patch.timestamp,
                    )
                    evt = DomainEvent.create(
                        printer_id=printer_id,
                        event_type="print.resumed",
                        severity=EventSeverity.INFO,
                        payload={"job_id": active_job.id, "progress": active_job.progress},
                        timestamp=patch.timestamp,
                    )
                    generated_events.append(evt)

                elif state.state in (PrinterState.COMPLETED,) or (patch.gcode_state and patch.gcode_state.upper() == "FINISH"):
                    active_job.transition_to(JobStatus.COMPLETED, patch.timestamp)
                    active_job.progress = 100
                    await self.job_repo.save(active_job)
                    evt = DomainEvent.create(
                        printer_id=printer_id,
                        event_type="print.completed",
                        severity=EventSeverity.INFO,
                        payload={
                            "job_id": active_job.id,
                            "filename": active_job.filename,
                            "duration_seconds": active_job.duration_seconds,
                            "total_layers": active_job.total_layers,
                        },
                        timestamp=patch.timestamp,
                    )
                    generated_events.append(evt)
                    self._active_jobs.pop(printer_id, None)
                    self._last_progress_marker.pop(printer_id, None)
                    active_job = None

                elif state.state == PrinterState.FAILED or (patch.gcode_state and patch.gcode_state.upper() == "FAILED"):
                    active_job.transition_to(JobStatus.FAILED, patch.timestamp)
                    await self.job_repo.save(active_job)
                    evt = DomainEvent.create(
                        printer_id=printer_id,
                        event_type="print.failed",
                        severity=EventSeverity.CRITICAL,
                        payload={
                            "job_id": active_job.id,
                            "filename": active_job.filename,
                            "progress": active_job.progress,
                            "error_code": patch.error_code,
                        },
                        timestamp=patch.timestamp,
                    )
                    generated_events.append(evt)
                    self._active_jobs.pop(printer_id, None)
                    self._last_progress_marker.pop(printer_id, None)
                    active_job = None

            # 6. Update in-memory state's PrintJobSnapshot
            if active_job:
                state.print = PrintJobSnapshot(
                    job_id=active_job.id,
                    filename=active_job.filename,
                    status=active_job.status.value,
                    progress=active_job.progress,
                    layer=active_job.layer,
                    total_layers=active_job.total_layers,
                    remaining_seconds=active_job.remaining_seconds,
                    started_at=active_job.started_at,
                )
            else:
                state.print = None

            # 7. Filament runout alert lifecycle
            runout_alerts = self._active_alerts.setdefault(printer_id, {})
            runout_alert = runout_alerts.get("filament.runout")
            if (
                patch.filament_runout is True
                and active_job is not None
                and state.state == PrinterState.PAUSED
                and runout_alert is None
            ):
                details = {
                    "job_id": active_job.id if active_job else None,
                    "filename": active_job.filename if active_job else None,
                    "progress": active_job.progress if active_job else None,
                    "layer": active_job.layer if active_job else None,
                    **patch.filament_runout_details,
                }
                runout_alert = Alert.create(
                    printer_id=printer_id,
                    alert_type="filament.runout",
                    severity=AlertSeverity.CRITICAL,
                    details=details,
                    created_at=patch.timestamp,
                )
                runout_alerts["filament.runout"] = runout_alert
                await self.alert_repo.save(runout_alert)
                generated_events.append(
                    DomainEvent.create(
                        printer_id=printer_id,
                        event_type="filament.runout",
                        severity=EventSeverity.CRITICAL,
                        payload={
                            "alert_id": runout_alert.id,
                            "alert_type": runout_alert.alert_type,
                            **details,
                        },
                        timestamp=patch.timestamp,
                    )
                )
            elif (
                runout_alert is not None
                and (patch.filament_runout is False or state.state == PrinterState.PRINTING)
            ):
                runout_alert.resolve(patch.timestamp)
                await self.alert_repo.save(runout_alert)
                runout_alerts.pop("filament.runout", None)
                generated_events.append(
                    DomainEvent.create(
                        printer_id=printer_id,
                        event_type="filament.runout_cleared",
                        severity=EventSeverity.INFO,
                        payload={
                            "alert_id": runout_alert.id,
                            "job_id": active_job.id if active_job else None,
                        },
                        timestamp=patch.timestamp,
                    )
                )

            # 8. Adaptive Multi-Factor Stall Detection
            if self.settings.detection.stall.enabled and active_job and active_job.status == JobStatus.RUNNING:
                stall_event = await self._evaluate_stall_detection(printer_id, active_job, patch.timestamp)
                if stall_event:
                    generated_events.append(stall_event)

            # 9. Emit all generated events
            for evt in generated_events:
                await self._emit_event(evt)

            return generated_events

    async def _evaluate_stall_detection(
        self,
        printer_id: str,
        job: PrintJob,
        now: datetime,
    ) -> Optional[DomainEvent]:
        """Evaluate adaptive multi-factor stall detection."""
        marker = self._last_progress_marker.get(printer_id)
        if not marker:
            self._last_progress_marker[printer_id] = (job.progress, job.layer, job.remaining_seconds, now)
            return None

        _, _, _, last_change_time = marker
        unchanged_seconds = (now - last_change_time).total_seconds()

        cfg = self.settings.detection.stall
        min_check = cfg.min_check_seconds
        adaptive_factor = cfg.adaptive_factor

        # Base timeout adjusted for long prints according to DESIGN.md §12.1
        total_estimated = job.duration_seconds + (job.remaining_seconds or 0)
        if total_estimated > 0:
            estimated_percent_duration = total_estimated / 100.0
        else:
            historical_duration = max(0, job.duration_seconds - unchanged_seconds)
            if job.progress > 0 and historical_duration > 0:
                estimated_percent_duration = historical_duration / job.progress
            elif job.total_layers > 0 and job.layer > 0 and historical_duration > 0:
                estimated_percent_duration = historical_duration / job.layer
            else:
                estimated_percent_duration = min_check

        timeout_threshold = max(min_check, estimated_percent_duration * adaptive_factor)

        if unchanged_seconds >= timeout_threshold:
            # Check if alert already ACTIVE or ACKNOWLEDGED
            alerts_map = self._active_alerts.setdefault(printer_id, {})
            if "print.possible_blockage" in alerts_map:
                # DUPLICATE SUPPRESSION: Condition continues, but alert is already active
                return None

            # First time detection -> Create Alert and emit domain event ONCE
            alert = Alert.create(
                printer_id=printer_id,
                alert_type="print.possible_blockage",
                severity=AlertSeverity.WARNING,
                details={
                    "job_id": job.id,
                    "progress": job.progress,
                    "layer": job.layer,
                    "unchanged_seconds": int(unchanged_seconds),
                    "threshold_seconds": int(timeout_threshold),
                },
                created_at=now,
            )
            alerts_map["print.possible_blockage"] = alert
            await self.alert_repo.save(alert)

            return DomainEvent.create(
                printer_id=printer_id,
                event_type="print.possible_blockage",
                severity=EventSeverity.WARNING,
                payload=alert.details,
                timestamp=now,
            )

        return None

    async def reconcile_on_startup(self, printer_id: str, initial_patch: Optional[TelemetryPatch] = None) -> None:
        """Reconcile in-memory state with existing SQLite print jobs on service startup."""
        async with self._printer_lock(printer_id):
            db_job = await self.job_repo.get_active_for_printer(printer_id)
            if db_job:
                # Check if printer is printing and matches this job
                printer_printing = False
                if initial_patch and initial_patch.state:
                    printer_printing = initial_patch.state in (PrinterState.PRINTING, PrinterState.PREPARING, PrinterState.PAUSED)
                elif initial_patch and initial_patch.gcode_state:
                    printer_printing = initial_patch.gcode_state.upper() in ("PREPARE", "RUNNING", "PAUSE")

                if printer_printing:
                    logger.info("Startup reconciliation: re-attaching to existing active job %s", db_job.id)
                    self._active_jobs[printer_id] = db_job
                    if initial_patch:
                        db_job.update_progress(
                            progress=initial_patch.progress,
                            layer=initial_patch.layer,
                            total_layers=initial_patch.total_layers,
                            remaining_seconds=initial_patch.remaining_seconds,
                            now=initial_patch.timestamp,
                        )
                        await self.job_repo.save(db_job)

                    state = self._states.setdefault(
                        printer_id,
                        CurrentPrinterState(printer_id=printer_id, model="A1"),
                    )
                    state.print = PrintJobSnapshot(
                        job_id=db_job.id,
                        filename=db_job.filename,
                        status=db_job.status.value,
                        progress=db_job.progress,
                        layer=db_job.layer,
                        total_layers=db_job.total_layers,
                        remaining_seconds=db_job.remaining_seconds,
                        started_at=db_job.started_at,
                    )
                else:
                    logger.info("Startup reconciliation: printer is idle; marking orphaned job %s as completed", db_job.id)
                    db_job.transition_to(JobStatus.COMPLETED)
                    await self.job_repo.save(db_job)

            # Notify startup reconciliation listeners (e.g. TimelapseManager)
            current_active = self._active_jobs.get(printer_id)
            for rec_listener in list(self._reconcile_listeners):
                try:
                    await rec_listener(printer_id, current_active)
                except Exception as exc:
                    logger.debug("Error in startup reconciliation listener: %s", exc)

    async def flush_state_to_db(self, printer_id: str) -> None:
        """Throttled periodic persistence of in-memory state snapshot."""
        # Snapshot under the printer's pipeline lock so a concurrent
        # apply_patch cannot produce a torn state persist.
        async with self._printer_lock(printer_id):
            state = self._states.get(printer_id)
            if not state:
                return
            try:
                snapshot = state.model_copy(deep=True)
            except Exception:
                snapshot = state
        if snapshot:
            await self.printer_repo.update_current_state(printer_id, snapshot)
