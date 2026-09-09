"""Integration tests for timelapse REST endpoints."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from httpx import AsyncClient

from bambu_monitor.timelapse.models import (
    TimelapsePause,
    TimelapseSession,
    TimelapseStatus,
)
from bambu_monitor.timelapse.storage import TimelapseStorage

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
FAKE_MP4 = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2mp41"


@pytest.mark.asyncio
async def test_list_timelapses_empty_and_populated(async_client: AsyncClient, tmp_path: Path):
    app = async_client._transport.app
    timelapse_repo = app.state.timelapse_repo

    # 1. Empty list for valid printer
    resp = await async_client.get("/api/v1/printers/test-a1/timelapses")
    assert resp.status_code == 200
    assert resp.json() == []

    # 2. 404 for unknown printer
    resp404 = await async_client.get("/api/v1/printers/unknown-printer/timelapses")
    assert resp404.status_code == 404

    # 3. Create a session and verify listing
    session_dir = tmp_path / "tl_session_1"
    session_dir.mkdir(parents=True, exist_ok=True)
    session = TimelapseSession(
        id="tl-session-1",
        printer_id="test-a1",
        print_job_id="job-101",
        subtask_name="Benchy",
        status=TimelapseStatus.COMPLETED,
        started_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 8, 12, 30, 0, tzinfo=timezone.utc),
        frame_count=150,
        fps=30,
        storage_dir=str(session_dir),
    )
    await timelapse_repo.save_session(session)

    resp = await async_client.get("/api/v1/printers/test-a1/timelapses")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == "tl-session-1"
    assert items[0]["frame_count"] == 150
    assert items[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_get_timelapse_detail(async_client: AsyncClient, tmp_path: Path):
    app = async_client._transport.app
    timelapse_repo = app.state.timelapse_repo
    storage: TimelapseStorage = app.state.timelapse_storage

    session_dir = tmp_path / "tl_session_detail"
    session_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession(
        id="tl-detail-1",
        printer_id="test-a1",
        print_job_id="job-202",
        camera_type="tapo_rtsp",
        status=TimelapseStatus.COMPLETED,
        started_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 8, 12, 30, 0, tzinfo=timezone.utc),
        frame_count=100,
        fps=30,
        storage_dir=str(session_dir),
    )
    await timelapse_repo.save_session(session)

    # Add pause record
    pause = TimelapsePause(
        session_id="tl-detail-1",
        started_at=datetime(2026, 9, 8, 12, 10, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 8, 12, 15, 0, tzinfo=timezone.utc),
        duration_seconds=300.0,
    )
    await timelapse_repo.save_pause(pause)

    # Save manifest via storage helper
    storage.save_manifest(session, session_dir)

    # 1. Successful get
    resp = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-detail-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "tl-detail-1"
    assert len(data["pauses"]) == 1
    assert data["pauses"][0]["duration_seconds"] == 300.0
    assert data["manifest"]["camera"]["type"] == "tapo_rtsp"

    # 2. 404 on nonexistent session
    resp_missing = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-nonexistent")
    assert resp_missing.status_code == 404

    # 3. 404 on printer mismatch
    resp_mismatch = await async_client.get("/api/v1/printers/test-a1-mini/timelapses/tl-detail-1")
    assert resp_mismatch.status_code == 404


@pytest.mark.asyncio
async def test_get_timelapse_video_download(async_client: AsyncClient, tmp_path: Path):
    app = async_client._transport.app
    timelapse_repo = app.state.timelapse_repo
    storage: TimelapseStorage = app.state.timelapse_storage

    session_dir = tmp_path / "tl_session_video"
    session_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession(
        id="tl-video-1",
        printer_id="test-a1",
        print_job_id="job-303",
        status=TimelapseStatus.COMPLETED,
        started_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc),
        storage_dir=str(session_dir),
    )
    await timelapse_repo.save_session(session)

    # Video not yet generated -> 404
    resp = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-video-1/video")
    assert resp.status_code == 404

    # Write dummy video
    video_path = storage.get_video_path(session_dir)
    video_path.write_bytes(FAKE_MP4)

    # Video now available -> 200 FileResponse
    resp = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-video-1/video")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == FAKE_MP4


@pytest.mark.asyncio
async def test_list_and_get_frames(async_client: AsyncClient, tmp_path: Path):
    app = async_client._transport.app
    timelapse_repo = app.state.timelapse_repo
    storage: TimelapseStorage = app.state.timelapse_storage

    session_dir = tmp_path / "tl_session_frames"
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession(
        id="tl-frames-1",
        printer_id="test-a1",
        print_job_id="job-404",
        status=TimelapseStatus.CAPTURING,
        started_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc),
        storage_dir=str(session_dir),
    )
    await timelapse_repo.save_session(session)

    # Save frames 1, 2, 3
    storage.save_frame(session_dir, 1, FAKE_JPEG)
    storage.save_frame(session_dir, 2, FAKE_JPEG)
    storage.save_frame(session_dir, 3, FAKE_JPEG)

    # List frames
    resp = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-frames-1/frames")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "tl-frames-1"
    assert data["total_frames"] == 3
    assert len(data["frames"]) == 3
    assert data["frames"][0]["sequence"] == 1
    assert data["frames"][0]["filename"] == "000001.jpg"

    # Get single frame
    resp_frame = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-frames-1/frames/2")
    assert resp_frame.status_code == 200
    assert resp_frame.headers["content-type"] == "image/jpeg"
    assert resp_frame.content == FAKE_JPEG

    # 404 for missing frame
    resp_missing_frame = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-frames-1/frames/999")
    assert resp_missing_frame.status_code == 404


@pytest.mark.asyncio
async def test_get_metadata_and_html_views(async_client: AsyncClient, tmp_path: Path):
    app = async_client._transport.app
    timelapse_repo = app.state.timelapse_repo
    storage: TimelapseStorage = app.state.timelapse_storage

    session_dir = tmp_path / "tl_session_view"
    session_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession(
        id="tl-view-1",
        printer_id="test-a1",
        print_job_id="job-view-1",
        status=TimelapseStatus.COMPLETED,
        frame_count=20,
        fps=30,
        storage_dir=str(session_dir),
    )
    await timelapse_repo.save_session(session)

    # Record metadata lines
    storage.append_frame_metadata(session_dir, {
        "frame": 1,
        "filename": "000001.jpg",
        "reason": "layer_change",
        "layer": 1,
        "progress": 5.0,
    })
    storage.append_frame_metadata(session_dir, {
        "frame": 2,
        "filename": "000002.jpg",
        "reason": "interval",
        "layer": 1,
        "progress": 7.5,
    })

    # 1. Metadata endpoint
    resp_meta = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-view-1/metadata")
    assert resp_meta.status_code == 200
    meta_records = resp_meta.json()
    assert len(meta_records) == 2
    assert meta_records[0]["reason"] == "layer_change"
    assert meta_records[0]["layer"] == 1
    assert meta_records[1]["reason"] == "interval"

    # 2. Correlation endpoint
    resp_corr = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-view-1/correlation")
    assert resp_corr.status_code == 200
    corr_data = resp_corr.json()
    assert corr_data["total_frames"] == 2
    assert "thermal_summary" in corr_data
    assert "timeline" in corr_data
    assert len(corr_data["timeline"]) == 2

    # Filter out timeline
    resp_corr_no_tl = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-view-1/correlation?include_timeline=false")
    assert resp_corr_no_tl.status_code == 200
    assert "timeline" not in resp_corr_no_tl.json()

    # 3. View HTML endpoint with HUD and SVG
    resp_view = await async_client.get("/api/v1/printers/test-a1/timelapses/tl-view-1/view")
    assert resp_view.status_code == 200
    assert "text/html" in resp_view.headers["content-type"]
    assert "tl-view-1" in resp_view.text
    assert "<video" in resp_view.text
    assert "hud-nozzle" in resp_view.text
    assert "timeline-svg" in resp_view.text
    assert "correlation-data" in resp_view.text
    assert "Download MP4" in resp_view.text

    # 4. Gallery HTML endpoint
    resp_gallery = await async_client.get("/api/v1/printers/test-a1/timelapses/gallery")
    assert resp_gallery.status_code == 200
    assert "text/html" in resp_gallery.headers["content-type"]
    assert "Timelapse Gallery" in resp_gallery.text
    assert "tl-view-1" in resp_gallery.text


