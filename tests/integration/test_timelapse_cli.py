"""Integration tests for timelapse CLI commands."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from bambu_monitor.camera.models import CameraHealth
from bambu_monitor.cli.commands import (
    cmd_timelapse_camera_test,
    cmd_timelapse_correlate,
    cmd_timelapse_generate,
    cmd_timelapse_list,
    cmd_timelapse_status,
)
from bambu_monitor.config import (
    ApplicationConfig,
    CameraConfig,
    DatabaseConfig,
    PrinterConfig,
    Settings,
    TimelapseCameraConfig,
    TimelapseConfig,
)
from bambu_monitor.domain.printer import Printer
from bambu_monitor.storage.database import Database
from bambu_monitor.storage.repositories import PrinterRepository, TimelapseRepository
from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
from bambu_monitor.timelapse.storage import TimelapseStorage

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"


@pytest.fixture
def cli_settings(tmp_path: Path) -> Settings:
    db_path = str(tmp_path / "cli_timelapse.db")
    storage_dir = str(tmp_path / "timelapses")
    return Settings(
        application=ApplicationConfig(environment="testing"),
        database=DatabaseConfig(path=db_path),
        timelapse=TimelapseConfig(
            storage_dir=storage_dir,
            camera=TimelapseCameraConfig(
                type="tapo_rtsp",
                stream="stream1",
                url="rtsp://user:pass@192.168.1.50:554/stream1",
            ),
        ),
        printers=[
            PrinterConfig(
                id="printer-1",
                model="A1",
                host="192.168.1.10",
                serial_number="01P001",
                timelapse=TimelapseConfig(enabled=True),
            )
        ],
    )


@pytest.mark.asyncio
async def test_cmd_timelapse_status(cli_settings: Settings, capsys):
    db = Database(db_path=cli_settings.database.path)
    await db.init_db()
    p_repo = PrinterRepository(db)
    tl_repo = TimelapseRepository(db)

    await p_repo.save(Printer(id="printer-1", model="A1", serial_number="01P001", host="192.168.1.10"))

    # Initial status - idle
    await cmd_timelapse_status(settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Timelapse Status" in captured
    assert "Active Session:    None (idle)" in captured

    # Create active session
    session = TimelapseSession(
        id="tl-active-1",
        printer_id="printer-1",
        print_job_id="job-99",
        status=TimelapseStatus.CAPTURING,
        frame_count=42,
        missed_frames=1,
    )
    await tl_repo.save_session(session)

    await cmd_timelapse_status(printer_id="printer-1", settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Active Session:    tl-active-1" in captured
    assert "Status:            CAPTURING" in captured
    assert "Frames Captured:   42 (1 missed)" in captured


@pytest.mark.asyncio
async def test_cmd_timelapse_list(cli_settings: Settings, capsys):
    db = Database(db_path=cli_settings.database.path)
    await db.init_db()
    p_repo = PrinterRepository(db)
    tl_repo = TimelapseRepository(db)

    await p_repo.save(Printer(id="printer-1", model="A1", serial_number="01P001", host="192.168.1.10"))

    # Empty list
    await cmd_timelapse_list(settings=cli_settings)
    captured = capsys.readouterr().out
    assert "No timelapse sessions recorded yet" in captured

    # Populate session
    session = TimelapseSession(
        id="tl-list-1",
        printer_id="printer-1",
        print_job_id="job-list-1",
        status=TimelapseStatus.COMPLETED,
        frame_count=120,
    )
    await tl_repo.save_session(session)

    await cmd_timelapse_list(settings=cli_settings)
    captured = capsys.readouterr().out
    assert "tl-list-1" in captured
    assert "printer-1" in captured
    assert "completed" in captured
    assert "120" in captured


@pytest.mark.asyncio
async def test_cmd_timelapse_camera_test_success(cli_settings: Settings, capsys):
    health = CameraHealth(
        connected=True,
        latency_ms=120.5,
        resolution="1920x1080",
        codec="h264",
        fps="15.0 fps",
    )

    with patch("bambu_monitor.cli.commands.create_camera_client") as mock_create:
        mock_client = AsyncMock()
        mock_client.health.return_value = health
        mock_client.capture.return_value = FAKE_JPEG
        mock_create.return_value = mock_client

        await cmd_timelapse_camera_test(printer_id="printer-1", settings=cli_settings)

    captured = capsys.readouterr().out
    assert "Camera: Tapo RTSP" in captured
    assert "Stream: stream1" in captured
    assert "Connection: OK" in captured
    assert "Snapshot: OK" in captured
    assert "1920x1080" in captured


@pytest.mark.asyncio
async def test_cmd_timelapse_camera_test_no_url(cli_settings: Settings, capsys):
    cli_settings.timelapse.camera.url = ""
    await cmd_timelapse_camera_test(printer_id="printer-1", settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Error: No RTSP camera URL configured" in captured


@pytest.mark.asyncio
async def test_cmd_timelapse_generate(cli_settings: Settings, tmp_path: Path, capsys):
    db = Database(db_path=cli_settings.database.path)
    await db.init_db()
    p_repo = PrinterRepository(db)
    tl_repo = TimelapseRepository(db)
    storage = TimelapseStorage(base_dir=cli_settings.timelapse.storage_dir)

    await p_repo.save(Printer(id="printer-1", model="A1", serial_number="01P001", host="192.168.1.10"))

    # 1. Nonexistent session
    await cmd_timelapse_generate("nonexistent-id", settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Error: Timelapse session not found" in captured

    # 2. Session with no frames
    session_dir = tmp_path / "tl_gen_empty"
    session_dir.mkdir(parents=True, exist_ok=True)
    session = TimelapseSession(
        id="tl-gen-1",
        printer_id="printer-1",
        print_job_id="job-gen-1",
        status=TimelapseStatus.FAILED,
        storage_dir=str(session_dir),
    )
    await tl_repo.save_session(session)

    await cmd_timelapse_generate("tl-gen-1", settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Error: No frames found for session" in captured

    # 3. Session with frames -> mock renderer
    storage.save_frame(session_dir, 1, FAKE_JPEG)
    storage.save_frame(session_dir, 2, FAKE_JPEG)

    fake_video = session_dir / "timelapse.mp4"
    fake_video.write_bytes(b"\x00\x00\x00 ftypisom")

    with patch("bambu_monitor.timelapse.renderer.TimelapseRenderer.render", new_callable=AsyncMock) as mock_render:
        mock_render.return_value = fake_video
        await cmd_timelapse_generate("tl-gen-1", settings=cli_settings)

    captured = capsys.readouterr().out
    assert "Generating timelapse video for session tl-gen-1 (2 frames)" in captured
    assert "Video compiled successfully" in captured


@pytest.mark.asyncio
async def test_cmd_timelapse_correlate(cli_settings: Settings, tmp_path: Path, capsys):
    db = Database(db_path=cli_settings.database.path)
    await db.init_db()
    p_repo = PrinterRepository(db)
    tl_repo = TimelapseRepository(db)
    storage = TimelapseStorage(base_dir=cli_settings.timelapse.storage_dir)

    await p_repo.save(Printer(id="printer-1", model="A1", serial_number="01P001", host="192.168.1.10"))

    # 1. Nonexistent session
    await cmd_timelapse_correlate("printer-1", "nonexistent-id", settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Error: Timelapse session not found" in captured

    # 2. Existing session with frames and telemetry
    session_dir = tmp_path / "tl_corr_cli"
    session_dir.mkdir(parents=True, exist_ok=True)
    session = TimelapseSession(
        id="tl-corr-cli-1",
        printer_id="printer-1",
        print_job_id="job-bench-1",
        status=TimelapseStatus.COMPLETED,
        frame_count=5,
        fps=30,
        storage_dir=str(session_dir),
    )
    await tl_repo.save_session(session)

    # Append frames with normal temps and one with a drop
    for i in range(1, 6):
        storage.append_frame_metadata(session_dir, {
            "frame": i,
            "filename": f"{i:06d}.jpg",
            "timestamp": datetime(2026, 9, 9, 10, 0, i * 5, tzinfo=timezone.utc).isoformat(),
            "nozzle_temp": 185.0 if i == 3 else 220.0,
            "nozzle_target": 220.0,
            "bed_temp": 60.0,
            "bed_target": 60.0,
            "layer": 1 if i <= 3 else 2,
            "progress": i * 20.0,
            "speed_percent": 100,
            "printer_state": "printing",
        })

    await cmd_timelapse_correlate("printer-1", "tl-corr-cli-1", settings=cli_settings)
    captured = capsys.readouterr().out
    assert "Bambu Monitor — Telemetry & Vision Correlation" in captured
    assert "tl-corr-cli-1" in captured
    assert "Total Frames: 5" in captured
    assert "Hotend:     Min: 185.0°C | Max: 220.0°C" in captured
    assert "Bed:        Min: 60.0°C | Max: 60.0°C" in captured
    assert "Detected Anomalies (1):" in captured
    assert "Frame 3" in captured
    assert "Layer Breakdown (2 layers):" in captured
    assert "1–3" in captured
    assert "4–5" in captured

