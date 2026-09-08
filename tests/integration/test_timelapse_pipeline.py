"""End-to-end integration test for the timelapse lifecycle pipeline."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from bambu_monitor.camera import CameraConfig, CameraConnectionError, CameraRegistry, TapoRTSPCamera
from bambu_monitor.config import ApplicationConfig, DatabaseConfig, PrinterConfig, Settings, TimelapseConfig
from bambu_monitor.domain.events import DomainEvent, EventSeverity
from bambu_monitor.domain.printer import Printer
from bambu_monitor.domain.telemetry import TelemetryPatch
from bambu_monitor.state.manager import StateManager
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import (
    AlertRepository,
    EventRepository,
    JobRepository,
    OutboxRepository,
    PrinterRepository,
    TimelapseRepository,
)
from bambu_monitor.timelapse.manager import TimelapseManager
from bambu_monitor.timelapse.models import TimelapseStatus
from bambu_monitor.timelapse.renderer import TimelapseRenderer
from bambu_monitor.timelapse.storage import TimelapseStorage

MINI_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00'
    b'\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19'
    b'\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $\x1e.\' \",#\x1c\x1c(7),01444'
    b'\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00'
    b'\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01'
    b'\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
)


@pytest.mark.asyncio
async def test_full_timelapse_lifecycle_pipeline(tmp_path: Path):
    db_path = str(tmp_path / "test_pipeline.db")
    db = Database(db_path)
    await db.init_db()

    printer_repo = PrinterRepository(db)
    job_repo = JobRepository(db)
    alert_repo = AlertRepository(db)
    event_repo = EventRepository(db)
    outbox_repo = OutboxRepository(db)
    timelapse_repo = TimelapseRepository(db)

    printer_id = "test-pipeline-printer"
    await printer_repo.save(Printer(id=printer_id, model="A1", serial_number="SN_PIPE", host="10.0.0.1"))

    settings = Settings(
        printers=[
            PrinterConfig(
                id=printer_id,
                model="A1",
                camera=CameraConfig(enabled=True, rtsp_url="rtsp://admin:pass@10.0.0.1:554/stream1"),
            )
        ],
        timelapse=TimelapseConfig(
            enabled=True,
            storage_dir=str(tmp_path / "timelapses"),
            capture={"interval_seconds": 0.04},
        ),
    )

    state_manager = StateManager(
        settings=settings,
        printer_repo=printer_repo,
        job_repo=job_repo,
        alert_repo=alert_repo,
        event_repo=event_repo,
        outbox_repo=outbox_repo,
    )
    state_manager.register_printer(printer_id, model="A1")

    storage = TimelapseStorage(base_dir=tmp_path / "timelapses")

    # Camera mock with disconnect behavior
    cam_mock = AsyncMock(spec=TapoRTSPCamera)
    cam_mock.sanitized_url = "rtsp://admin:***@10.0.0.1:554/stream1"
    # Call 1: success, Call 2: disconnect, Call 3: reconnect success
    cam_mock.capture.side_effect = [
        MINI_JPEG,
        CameraConnectionError("Wi-Fi glitch"),
        MINI_JPEG,
        MINI_JPEG,
        MINI_JPEG,
    ]

    registry = CameraRegistry()
    registry.register(printer_id, cam_mock)

    renderer_mock = AsyncMock(spec=TimelapseRenderer)
    async def fake_render(session, storage, **kwargs):
        vpath = Path(session.storage_dir) / "timelapse.mp4"
        vpath.write_bytes(b"final mp4 video content")
        session.video_path = str(vpath)
        session.transition_to(TimelapseStatus.COMPLETED)
        return vpath
    renderer_mock.render.side_effect = fake_render

    timelapse_manager = TimelapseManager(
        settings=settings,
        timelapse_repo=timelapse_repo,
        storage=storage,
        camera_registry=registry,
        renderer=renderer_mock,
        emit_event_cb=state_manager._emit_event,
    )

    state_manager.add_event_listener(timelapse_manager.handle_domain_event)
    state_manager.add_reconcile_listener(timelapse_manager.reconcile_on_startup)

    t0 = datetime(2026, 9, 8, 14, 0, 0, tzinfo=timezone.utc)

    # 1. State: IDLE -> Telemetry: print starts (RUNNING)
    patch1 = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t0,
        gcode_state="RUNNING",
        subtask_name="benchy.3mf",
        progress=1,
        layer=1,
        total_layers=100,
        online=True,
    )
    events = await state_manager.apply_patch(patch1)
    assert any(e.event_type == "print.started" for e in events)

    session = timelapse_manager.get_active_session(printer_id)
    assert session is not None
    assert session.status in (TimelapseStatus.CAPTURING, TimelapseStatus.DEGRADED)

    # Wait for a couple capture ticks
    await asyncio.sleep(0.12)
    frames_mid = session.frame_count
    assert frames_mid >= 1

    # 2. Telemetry: Print pauses
    t1 = datetime(2026, 9, 8, 14, 10, 0, tzinfo=timezone.utc)
    patch_pause = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t1,
        gcode_state="PAUSE",
        progress=25,
        layer=25,
        total_layers=100,
    )
    events = await state_manager.apply_patch(patch_pause)
    assert any(e.event_type == "print.paused" for e in events)
    assert session.status == TimelapseStatus.PAUSED

    # Wait a small tick and record frame count while paused
    await asyncio.sleep(0.02)
    frames_paused = session.frame_count

    # Ensure no frames captured while paused
    await asyncio.sleep(0.1)
    assert session.frame_count == frames_paused

    # 3. Telemetry: Print resumes
    t2 = datetime(2026, 9, 8, 14, 15, 0, tzinfo=timezone.utc)
    patch_resume = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t2,
        gcode_state="RUNNING",
        progress=26,
        layer=26,
        total_layers=100,
    )
    events = await state_manager.apply_patch(patch_resume)
    assert any(e.event_type == "print.resumed" for e in events)
    assert session.status == TimelapseStatus.CAPTURING

    # Wait for capture to continue
    await asyncio.sleep(0.12)
    assert session.frame_count > frames_paused

    # 4. Telemetry: Print completes (FINISH)
    t3 = datetime(2026, 9, 8, 14, 30, 0, tzinfo=timezone.utc)
    patch_finish = TelemetryPatch(
        printer_id=printer_id,
        timestamp=t3,
        gcode_state="FINISH",
        progress=100,
        layer=100,
        total_layers=100,
    )
    events = await state_manager.apply_patch(patch_finish)
    assert any(e.event_type == "print.completed" for e in events)

    # Shutdown manager to await video generation background task
    await timelapse_manager.shutdown()

    # 5. Verify final session in DB and storage
    saved_session = await timelapse_repo.get_session(session.id)
    assert saved_session is not None
    assert saved_session.status == TimelapseStatus.COMPLETED
    assert saved_session.video_path is not None
    assert Path(saved_session.video_path).is_file()

    # Verify manifest on disk
    manifest = storage.load_manifest(saved_session.storage_dir)
    assert manifest is not None
    assert manifest.status == "completed"
    assert manifest.frame_count >= 2

    # Verify frames.jsonl sidecar records
    metadata_records = storage.read_frames_metadata(saved_session.storage_dir)
    assert len(metadata_records) >= 2
    assert metadata_records[0]["frame"] == 1
    assert "filename" in metadata_records[0]

    # Verify outbox delivery queue has timelapse.completed with timelapse_video_url for Hermes
    outbox_messages = await outbox_repo.list_pending(printer_id, limit=50)
    tl_completed_msg = next((m for m in outbox_messages if m.payload.get("event_type") == "timelapse.completed"), None)
    assert tl_completed_msg is not None
    evt_payload = tl_completed_msg.payload.get("payload", {})
    assert "timelapse_video_url" in evt_payload
    assert f"/timelapses/{session.id}/video" in evt_payload["timelapse_video_url"]

