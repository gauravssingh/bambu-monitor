"""Concurrency tests for TimelapseManager's per-printer locking and shutdown safety.

These specifically verify the Phase 3 locking-granularity change: printer A's
lifecycle handling must never wait on printer B's, same-printer operations
must still be strictly serialized, and shutdown must be race-free against
in-flight and newly-arriving lifecycle events.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from bambu_monitor.camera import CameraConfig, CameraRegistry, TapoRTSPCamera
from bambu_monitor.config import PrinterConfig, Settings, TimelapseConfig
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import PrinterRepository, TimelapseRepository
from bambu_monitor.timelapse.manager import TimelapseManager
from bambu_monitor.timelapse.renderer import TimelapseRenderer

MINI_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00'
    b'\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19'
    b'\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $\x1e.\' \",#\x1c\x1c(7),01444'
    b'\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00'
    b'\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01'
    b'\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
)


@pytest.fixture
async def two_printer_env(tmp_path: Path):
    from bambu_monitor.timelapse.storage import TimelapseStorage
    from bambu_monitor.domain.printer import Printer

    db = Database(str(tmp_path / "test_concurrency.db"))
    await db.init_db()

    p_repo = PrinterRepository(db)
    await p_repo.save(Printer(id="printer-a", model="A1", serial_number="SN_A", host="10.0.0.10"))
    await p_repo.save(Printer(id="printer-b", model="A1", serial_number="SN_B", host="10.0.0.11"))

    repo = TimelapseRepository(db)
    storage = TimelapseStorage(base_dir=tmp_path / "timelapses")

    settings = Settings(
        printers=[
            PrinterConfig(id="printer-a", model="A1", camera=CameraConfig(enabled=True, rtsp_url="rtsp://x/a")),
            PrinterConfig(id="printer-b", model="A1", camera=CameraConfig(enabled=True, rtsp_url="rtsp://x/b")),
        ],
        timelapse=TimelapseConfig(enabled=True, storage_dir=str(tmp_path / "timelapses")),
    )

    registry = CameraRegistry()
    for pid in ("printer-a", "printer-b"):
        cam = AsyncMock(spec=TapoRTSPCamera)
        cam.capture.return_value = MINI_JPEG
        cam.sanitized_url = f"rtsp://x/{pid}"
        registry.register(pid, cam)

    renderer_mock = AsyncMock(spec=TimelapseRenderer)

    manager = TimelapseManager(
        settings=settings,
        timelapse_repo=repo,
        storage=storage,
        camera_registry=registry,
        renderer=renderer_mock,
    )

    return {"manager": manager, "repo": repo}


@pytest.mark.asyncio
async def test_slow_printer_a_does_not_block_printer_b(two_printer_env):
    """A slow persistence write for printer A's session creation must not
    delay printer B's session creation — they hold independent locks."""
    m: TimelapseManager = two_printer_env["manager"]
    repo = two_printer_env["repo"]

    a_unblocked = asyncio.Event()
    real_save_session = repo.save_session

    async def slow_save_session(session):
        if session.printer_id == "printer-a":
            await a_unblocked.wait()
        return await real_save_session(session)

    repo.save_session = slow_save_session  # type: ignore[assignment]

    task_a = asyncio.create_task(m.on_print_started("printer-a", "job-a", {"filename": "a.3mf"}))
    await asyncio.sleep(0.01)  # let task_a enter its lock and block on a_unblocked

    # printer B must complete promptly, without waiting on printer A's lock.
    session_b = await asyncio.wait_for(
        m.on_print_started("printer-b", "job-b", {"filename": "b.3mf"}), timeout=1.0
    )
    assert session_b is not None
    assert session_b.printer_id == "printer-b"

    a_unblocked.set()
    session_a = await asyncio.wait_for(task_a, timeout=1.0)
    assert session_a is not None
    assert session_a.printer_id == "printer-a"

    await m.shutdown()


@pytest.mark.asyncio
async def test_same_printer_operations_stay_serialized(two_printer_env):
    """Two concurrent print.started events for the SAME printer (different
    jobs, so neither short-circuits via the idempotency check) must never
    execute their critical sections concurrently, even though neither
    blocks the other printer."""
    m: TimelapseManager = two_printer_env["manager"]
    repo = two_printer_env["repo"]

    concurrent_count = 0
    max_observed = 0
    real_save_session = repo.save_session

    async def tracked_save_session(session):
        nonlocal concurrent_count, max_observed
        concurrent_count += 1
        max_observed = max(max_observed, concurrent_count)
        await asyncio.sleep(0.02)
        concurrent_count -= 1
        return await real_save_session(session)

    repo.save_session = tracked_save_session  # type: ignore[assignment]

    results = await asyncio.gather(
        m.on_print_started("printer-a", "job-x1", {"filename": "x1.3mf"}),
        m.on_print_started("printer-a", "job-x2", {"filename": "x2.3mf"}),
    )

    assert max_observed == 1, "two same-printer operations ran their critical sections concurrently"
    assert results[0] is not None and results[1] is not None
    assert {results[0].print_job_id, results[1].print_job_id} == {"job-x1", "job-x2"}
    # The second job supersedes the first as the printer's active session.
    assert m.get_active_session("printer-a").print_job_id == "job-x2"

    await m.shutdown()


@pytest.mark.asyncio
async def test_shutdown_rejects_lifecycle_events_once_started(two_printer_env):
    """Once shutdown() has been invoked, later lifecycle calls must be
    no-ops rather than creating new sessions/workers."""
    m: TimelapseManager = two_printer_env["manager"]

    await m.shutdown()
    session = await m.on_print_started("printer-a", "job-after-shutdown", {"filename": "late.3mf"})

    assert session is None
    assert m.get_active_session("printer-a") is None


@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_handler_before_stopping_worker(two_printer_env):
    """shutdown() must not stop a printer's worker while a handler for that
    same printer is still inside its critical section."""
    m: TimelapseManager = two_printer_env["manager"]
    repo = two_printer_env["repo"]

    session = await m.on_print_started("printer-a", "job-inflight", {"filename": "f.3mf"})
    assert session is not None
    worker = m._workers["printer-a"]

    started_pause = asyncio.Event()
    finish_pause = asyncio.Event()
    real_save_session = repo.save_session

    async def blocking_save_session(sess):
        started_pause.set()
        await finish_pause.wait()
        return await real_save_session(sess)

    repo.save_session = blocking_save_session  # type: ignore[assignment]

    pause_task = asyncio.create_task(m.on_print_paused("printer-a", {}))
    await asyncio.wait_for(started_pause.wait(), timeout=1.0)

    # shutdown() must block on printer-a's lock, not race past on_print_paused.
    shutdown_task = asyncio.create_task(m.shutdown())
    await asyncio.sleep(0.01)
    assert not shutdown_task.done(), "shutdown() proceeded without waiting for the in-flight handler"
    assert worker.is_paused is True  # pause() itself already ran before the blocking save

    finish_pause.set()
    await asyncio.wait_for(pause_task, timeout=1.0)
    await asyncio.wait_for(shutdown_task, timeout=1.0)


@pytest.mark.asyncio
async def test_concurrent_shutdown_and_multi_printer_events_do_not_deadlock(two_printer_env):
    """Racing print-started events for two different printers against
    shutdown() itself must resolve within a bounded time. Which events win
    the race against the shutdown flag is legitimately undefined — the only
    requirement is that nothing hangs or raises."""
    m: TimelapseManager = two_printer_env["manager"]

    await asyncio.wait_for(
        asyncio.gather(
            m.on_print_started("printer-a", "job-a2", {"filename": "a2.3mf"}),
            m.on_print_started("printer-b", "job-b2", {"filename": "b2.3mf"}),
            m.shutdown(),
        ),
        timeout=2.0,
    )
