"""Unit tests for TimelapseManager state machine and event handling."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from bambu_monitor.camera import CameraConfig, CameraRegistry, TapoRTSPCamera
from bambu_monitor.config import PrinterConfig, Settings, TimelapseConfig
from bambu_monitor.domain.events import DomainEvent, EventSeverity
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import TimelapseRepository
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


@pytest.fixture
async def setup_manager(tmp_path: Path):
    db_path = str(tmp_path / "test_tl.db")
    db = Database(db_path)
    await db.init_db()

    from bambu_monitor.domain.printer import Printer
    from bambu_monitor.storage.repositories import PrinterRepository
    p_repo = PrinterRepository(db)
    await p_repo.save(Printer(id="printer-1", model="A1", serial_number="SN_TEST", host="10.0.0.5"))

    repo = TimelapseRepository(db)
    storage = TimelapseStorage(base_dir=tmp_path / "timelapses")

    settings = Settings(
        printers=[
            PrinterConfig(
                id="printer-1",
                model="A1",
                camera=CameraConfig(
                    enabled=True,
                    rtsp_url="rtsp://admin:secret@10.0.0.5:554/stream1",
                ),
            )
        ],
        timelapse=TimelapseConfig(
            enabled=True,
            storage_dir=str(tmp_path / "timelapses"),
        ),
    )

    cam_mock = AsyncMock(spec=TapoRTSPCamera)
    cam_mock.capture.return_value = MINI_JPEG
    cam_mock.sanitized_url = "rtsp://admin:***@10.0.0.5:554/stream1"

    registry = CameraRegistry()
    registry.register("printer-1", cam_mock)

    emitted_events = []
    async def emit_cb(event: DomainEvent):
        emitted_events.append(event)

    renderer_mock = AsyncMock(spec=TimelapseRenderer)
    async def fake_render(session, storage, **kwargs):
        vpath = Path(session.storage_dir) / "timelapse.mp4"
        vpath.write_bytes(b"fake mp4 video")
        session.video_path = str(vpath)
        session.transition_to(TimelapseStatus.COMPLETED)
        return vpath
    renderer_mock.render.side_effect = fake_render

    manager = TimelapseManager(
        settings=settings,
        timelapse_repo=repo,
        storage=storage,
        camera_registry=registry,
        renderer=renderer_mock,
        emit_event_cb=emit_cb,
    )

    return {
        "manager": manager,
        "repo": repo,
        "storage": storage,
        "camera": cam_mock,
        "renderer": renderer_mock,
        "events": emitted_events,
    }


@pytest.mark.asyncio
async def test_lifecycle_full_happy_path(setup_manager):
    m = setup_manager["manager"]
    repo = setup_manager["repo"]
    events = setup_manager["events"]

    # 1. print.started
    evt_start = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.started",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-benchy-1", "filename": "benchy.3mf"},
    )
    await m.handle_domain_event(evt_start)

    session = m.get_active_session("printer-1")
    assert session is not None
    assert session.status == TimelapseStatus.CAPTURING
    assert session.print_job_id == "job-benchy-1"
    assert any(e.event_type == "timelapse.started" for e in events)

    # 2. print.paused
    evt_pause = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.paused",
        severity=EventSeverity.WARNING,
        payload={"job_id": "job-benchy-1"},
    )
    await m.handle_domain_event(evt_pause)
    assert session.status == TimelapseStatus.PAUSED
    assert any(e.event_type == "timelapse.paused" for e in events)

    # 3. print.resumed
    evt_resume = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.resumed",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-benchy-1"},
    )
    await m.handle_domain_event(evt_resume)
    assert session.status == TimelapseStatus.CAPTURING
    assert any(e.event_type == "timelapse.resumed" for e in events)

    # Save a frame to ensure frames exist for video compilation
    setup_manager["storage"].save_frame(Path(session.storage_dir), 1, MINI_JPEG)

    # 4. print.completed
    evt_done = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.completed",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-benchy-1"},
    )
    await m.handle_domain_event(evt_done)

    # Wait for background render task
    await m.shutdown()

    # Session completed
    saved_session = await repo.get_session(session.id)
    assert saved_session is not None
    assert saved_session.status == TimelapseStatus.COMPLETED
    assert saved_session.video_path is not None
    assert any(e.event_type == "timelapse.completed" for e in events)


@pytest.mark.asyncio
async def test_idempotent_duplicate_start_events(setup_manager):
    m = setup_manager["manager"]
    repo = setup_manager["repo"]

    evt1 = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.started",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-duplicate-1", "filename": "test.3mf"},
    )
    await m.handle_domain_event(evt1)
    s1 = m.get_active_session("printer-1")

    # Send exact duplicate start
    await m.handle_domain_event(evt1)
    s2 = m.get_active_session("printer-1")

    assert s1.id == s2.id

    all_sessions = await repo.list_sessions_for_printer("printer-1")
    assert len(all_sessions) == 1
    assert all_sessions[0].id == s1.id

    await m.shutdown()


@pytest.mark.asyncio
async def test_harmless_duplicate_pause_and_resume(setup_manager):
    m = setup_manager["manager"]

    evt_start = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.started",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-pause-test", "filename": "test.3mf"},
    )
    await m.handle_domain_event(evt_start)

    evt_pause = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.paused",
        severity=EventSeverity.WARNING,
        payload={"job_id": "job-pause-test"},
    )
    await m.handle_domain_event(evt_pause)
    await m.handle_domain_event(evt_pause)  # duplicate pause

    s = m.get_active_session("printer-1")
    assert s.status == TimelapseStatus.PAUSED

    evt_resume = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.resumed",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-pause-test"},
    )
    await m.handle_domain_event(evt_resume)
    await m.handle_domain_event(evt_resume)  # duplicate resume

    assert s.status == TimelapseStatus.CAPTURING
    await m.shutdown()


@pytest.mark.asyncio
async def test_print_failed_finalizes_and_preserves_frames(setup_manager):
    m = setup_manager["manager"]
    repo = setup_manager["repo"]

    evt_start = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.started",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-failed-1", "filename": "fail.3mf"},
    )
    await m.handle_domain_event(evt_start)
    session = m.get_active_session("printer-1")

    # Simulate 1 frame
    setup_manager["storage"].save_frame(Path(session.storage_dir), 1, MINI_JPEG)

    evt_fail = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.failed",
        severity=EventSeverity.CRITICAL,
        payload={"job_id": "job-failed-1", "error_code": 105},
    )
    await m.handle_domain_event(evt_fail)
    await m.shutdown()

    saved_session = await repo.get_session(session.id)
    assert saved_session.status == TimelapseStatus.FAILED
    assert any(e.event_type == "timelapse.failed" for e in setup_manager["events"])


@pytest.mark.asyncio
async def test_print_layer_changed_triggers_capture(setup_manager):
    m: TimelapseManager = setup_manager["manager"]
    # Configure hybrid mode directly on settings
    m.settings.timelapse.capture.mode = "hybrid"

    evt_start = DomainEvent.create(
        printer_id="printer-1",
        event_type="print.started",
        severity=EventSeverity.INFO,
        payload={"job_id": "job-layer-test", "filename": "layer.3mf"},
    )
    await m.handle_domain_event(evt_start)

    worker = m._workers.get("printer-1")
    assert worker is not None

    with patch.object(worker, "trigger_capture", new_callable=AsyncMock) as mock_trigger:
        evt_layer = DomainEvent.create(
            printer_id="printer-1",
            event_type="print.layer_changed",
            severity=EventSeverity.INFO,
            payload={"job_id": "job-layer-test", "layer": 5, "progress": 10.0},
        )
        await m.handle_domain_event(evt_layer)

        # Layer captures are scheduled as background tasks (so the event
        # pipeline is never blocked by a multi-second RTSP round-trip);
        # flush the loop to let the pending task run.
        if m._capture_tasks:
            await asyncio.gather(*list(m._capture_tasks))

        mock_trigger.assert_awaited_once_with(
            reason="layer_change",
            metadata={"layer": 5, "progress": 10.0},
        )

    await m.shutdown()

