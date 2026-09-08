"""Unit tests for FrameCaptureWorker (periodic capture, pause/resume, camera failure & recovery)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from bambu_monitor.camera import CameraConnectionError
from bambu_monitor.timelapse.capture import FrameCaptureWorker
from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
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
async def test_worker_periodic_capture(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_worker_capture"
    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        capture_interval_seconds=0.05,
        storage_dir=str(session_dir),
    )

    mock_camera = AsyncMock()
    mock_camera.capture.return_value = MINI_JPEG

    captured_frames = []
    async def on_frame(seq, path):
        captured_frames.append((seq, path))

    worker = FrameCaptureWorker(
        session=session,
        camera=mock_camera,
        storage=storage,
        on_frame_captured=on_frame,
    )
    worker.start()

    # Wait for ~3 captures
    await asyncio.sleep(0.18)
    await worker.stop()

    assert session.frame_count >= 2
    assert len(captured_frames) >= 2
    assert storage.get_frame_path(session_dir, 1).is_file()
    assert storage.get_frame_path(session_dir, 2).is_file()


@pytest.mark.asyncio
async def test_worker_pause_and_resume(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_worker_pause"
    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        capture_interval_seconds=0.05,
        storage_dir=str(session_dir),
    )

    mock_camera = AsyncMock()
    mock_camera.capture.return_value = MINI_JPEG

    worker = FrameCaptureWorker(
        session=session,
        camera=mock_camera,
        storage=storage,
    )
    worker.start()
    await asyncio.sleep(0.12)
    count_before_pause = session.frame_count
    assert count_before_pause >= 1

    # Pause worker
    worker.pause()
    assert worker.is_paused is True
    await asyncio.sleep(0.15)
    # No new frames should be captured while paused
    assert session.frame_count == count_before_pause

    # Resume worker
    worker.resume()
    assert worker.is_paused is False
    await asyncio.sleep(0.12)
    assert session.frame_count > count_before_pause

    await worker.stop()


@pytest.mark.asyncio
async def test_worker_camera_failure_and_recovery(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_worker_degrade"
    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        capture_interval_seconds=0.05,
        storage_dir=str(session_dir),
    )

    mock_camera = AsyncMock()
    # Sequence of captures: 1 success, 2 failures (outage), 1 recovery success
    mock_camera.capture.side_effect = [
        MINI_JPEG,
        CameraConnectionError("Stream disconnected"),
        CameraConnectionError("Stream disconnected"),
        MINI_JPEG,
        MINI_JPEG,
    ]

    degraded_events = []
    recovered_events = []

    async def on_degraded(err):
        degraded_events.append(err)

    async def on_recovered():
        recovered_events.append(True)

    worker = FrameCaptureWorker(
        session=session,
        camera=mock_camera,
        storage=storage,
        on_degraded=on_degraded,
        on_recovered=on_recovered,
    )
    worker.start()

    await asyncio.sleep(0.25)
    await worker.stop()

    assert session.missed_frames >= 2
    assert session.camera_outage_count >= 1
    assert len(degraded_events) >= 1
    assert len(recovered_events) >= 1
    # After recovery, session should be CAPTURING again
    assert session.status == TimelapseStatus.CAPTURING


@pytest.mark.asyncio
async def test_worker_layer_trigger_and_metadata_sidecar(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_worker_trigger"
    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-trigger",
        capture_interval_seconds=10.0,
        storage_dir=str(session_dir),
    )

    mock_camera = AsyncMock()
    mock_camera.capture.return_value = MINI_JPEG

    worker = FrameCaptureWorker(
        session=session,
        camera=mock_camera,
        storage=storage,
        mode="layer",
    )
    worker.start()

    # In layer mode, periodic timer does not capture every tick
    await asyncio.sleep(0.05)
    assert session.frame_count == 0

    # Explicit trigger_capture on layer change
    res1 = await worker.trigger_capture(reason="layer_change", metadata={"layer": 10, "progress": 15.0})
    assert res1 is not None
    assert session.frame_count == 1

    # Immediate trigger should be debounced
    res_debounced = await worker.trigger_capture(reason="layer_change", metadata={"layer": 10})
    assert res_debounced is None
    assert session.frame_count == 1

    await worker.stop()

    # Verify frames.jsonl was written
    records = storage.read_frames_metadata(session_dir)
    assert len(records) == 1
    assert records[0]["frame"] == 1
    assert records[0]["reason"] == "layer_change"
    assert records[0]["layer"] == 10
    assert records[0]["progress"] == 15.0

