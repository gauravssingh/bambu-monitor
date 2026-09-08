"""Integration tests for Timelapse restart recovery and reconciliation."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from bambu_monitor.camera import CameraConfig, CameraRegistry, TapoRTSPCamera
from bambu_monitor.config import Settings, TimelapseConfig
from bambu_monitor.domain.printer import Printer
from bambu_monitor.domain.print_job import JobStatus, PrintJob
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import PrinterRepository, TimelapseRepository
from bambu_monitor.timelapse.manager import TimelapseManager
from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
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
async def setup_env(tmp_path: Path):
    db_path = str(tmp_path / "test_recovery.db")
    db = Database(db_path)
    await db.init_db()

    p_repo = PrinterRepository(db)
    await p_repo.save(Printer(id="printer-1", model="A1", serial_number="SN_REC", host="10.0.0.1"))

    tl_repo = TimelapseRepository(db)
    storage = TimelapseStorage(base_dir=tmp_path / "timelapses")

    settings = Settings(
        timelapse=TimelapseConfig(
            enabled=True,
            storage_dir=str(tmp_path / "timelapses"),
        )
    )

    cam_mock = AsyncMock(spec=TapoRTSPCamera)
    cam_mock.capture.return_value = MINI_JPEG

    registry = CameraRegistry()
    registry.register("printer-1", cam_mock)

    renderer_mock = AsyncMock(spec=TimelapseRenderer)
    async def fake_render(session, storage, **kwargs):
        vpath = Path(session.storage_dir) / "timelapse.mp4"
        vpath.write_bytes(b"rendered mp4")
        session.video_path = str(vpath)
        session.transition_to(TimelapseStatus.COMPLETED)
        return vpath
    renderer_mock.render.side_effect = fake_render

    def make_manager():
        return TimelapseManager(
            settings=settings,
            timelapse_repo=tl_repo,
            storage=storage,
            camera_registry=registry,
            renderer=renderer_mock,
        )

    return {
        "make_manager": make_manager,
        "repo": tl_repo,
        "storage": storage,
    }


@pytest.mark.asyncio
async def test_restart_while_capturing_resumes_without_duplicate_session(setup_env):
    repo = setup_env["repo"]
    storage = setup_env["storage"]
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Pre-restart: session was CAPTURING in database
    session_dir = storage.resolve_session_dir("printer-1", "session_running_1", t0)
    storage.ensure_session_dirs(session_dir)
    storage.save_frame(session_dir, 1, MINI_JPEG)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job_active_1",
        storage_dir=str(session_dir),
        started_at=t0,
    )
    session.id = "session_running_1"
    session.frame_count = 1
    await repo.save_session(session)
    storage.save_manifest(session)

    # Active print job on printer
    active_job = PrintJob.create(
        printer_id="printer-1",
        filename="model.3mf",
        status=JobStatus.RUNNING,
        started_at=t0,
    )
    active_job.id = "job_active_1"

    # 2. Simulate fresh service startup with new TimelapseManager
    m = setup_env["make_manager"]()
    await m.reconcile_on_startup("printer-1", active_job)

    # Verification:
    # Must re-attach to existing session
    attached = m.get_active_session("printer-1")
    assert attached is not None
    assert attached.id == "session_running_1"
    assert attached.status == TimelapseStatus.CAPTURING

    # Must NOT create a second session in DB
    all_sessions = await repo.list_sessions_for_printer("printer-1")
    assert len(all_sessions) == 1
    assert all_sessions[0].id == "session_running_1"

    await m.shutdown()


@pytest.mark.asyncio
async def test_restart_while_paused_maintains_paused_state(setup_env):
    repo = setup_env["repo"]
    storage = setup_env["storage"]
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    session_dir = storage.resolve_session_dir("printer-1", "session_paused_1", t0)
    storage.ensure_session_dirs(session_dir)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job_paused_1",
        storage_dir=str(session_dir),
        started_at=t0,
    )
    session.id = "session_paused_1"
    session.transition_to(TimelapseStatus.PAUSED)
    await repo.save_session(session)

    active_job = PrintJob.create(
        printer_id="printer-1",
        filename="model.3mf",
        status=JobStatus.PAUSED,
        started_at=t0,
    )
    active_job.id = "job_paused_1"

    # Restart
    m = setup_env["make_manager"]()
    await m.reconcile_on_startup("printer-1", active_job)

    attached = m.get_active_session("printer-1")
    assert attached is not None
    assert attached.id == "session_paused_1"
    assert attached.status == TimelapseStatus.PAUSED

    await m.shutdown()


@pytest.mark.asyncio
async def test_restart_during_finalizing_retries_render(setup_env):
    repo = setup_env["repo"]
    storage = setup_env["storage"]
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    session_dir = storage.resolve_session_dir("printer-1", "session_finalizing_1", t0)
    storage.ensure_session_dirs(session_dir)
    storage.save_frame(session_dir, 1, MINI_JPEG)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job_fin_1",
        storage_dir=str(session_dir),
        started_at=t0,
    )
    session.id = "session_finalizing_1"
    session.transition_to(TimelapseStatus.FINALIZING)
    await repo.save_session(session)

    # Active print job matches
    active_job = PrintJob.create(
        printer_id="printer-1",
        filename="model.3mf",
        status=JobStatus.RUNNING,
        started_at=t0,
    )
    active_job.id = "job_fin_1"

    m = setup_env["make_manager"]()
    await m.reconcile_on_startup("printer-1", active_job)

    # Wait for background retry render
    await m.shutdown()

    saved = await repo.get_session("session_finalizing_1")
    assert saved.status == TimelapseStatus.COMPLETED
    assert saved.video_path is not None


@pytest.mark.asyncio
async def test_restart_orphaned_session_on_idle_printer_is_finalized(setup_env):
    repo = setup_env["repo"]
    storage = setup_env["storage"]
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    session_dir = storage.resolve_session_dir("printer-1", "session_orphan_1", t0)
    storage.ensure_session_dirs(session_dir)
    storage.save_frame(session_dir, 1, MINI_JPEG)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job_orphan_1",
        storage_dir=str(session_dir),
        started_at=t0,
    )
    session.id = "session_orphan_1"
    await repo.save_session(session)

    # Restart when printer is IDLE (active_job is None)
    m = setup_env["make_manager"]()
    await m.reconcile_on_startup("printer-1", active_job=None)

    await m.shutdown()

    saved = await repo.get_session("session_orphan_1")
    assert saved is not None
    assert saved.status in (TimelapseStatus.COMPLETED, TimelapseStatus.FAILED)
