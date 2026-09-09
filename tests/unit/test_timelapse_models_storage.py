"""Unit tests for TimelapseSession, Pause, Manifest, and TimelapseStorage filesystem layout."""

from datetime import datetime, timezone
from pathlib import Path

from bambu_monitor.timelapse.models import (
    TimelapsePause,
    TimelapseSession,
    TimelapseStatus,
    generate_session_id,
)
from bambu_monitor.timelapse.storage import TimelapseStorage

MINI_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00'
    b'\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19'
    b'\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $\x1e.\' \",#\x1c\x1c(7),01444'
    b'\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00'
    b'\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01'
    b'\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
)


def test_session_id_generation():
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    session_id = generate_session_id("bambu-a1-mini", "job_benchy_123", t0)
    assert session_id.startswith("tl_bambu_a1_mini_job_benchy_123_")
    assert str(int(t0.timestamp())) in session_id


def test_session_state_transitions():
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        storage_dir="/tmp/session",
        started_at=t0,
    )
    assert session.status == TimelapseStatus.CAPTURING
    assert session.frame_count == 0
    assert session.paused_seconds == 0.0

    # Transition to PAUSED
    t_pause = datetime(2026, 9, 8, 12, 5, 0, tzinfo=timezone.utc)
    session.transition_to(TimelapseStatus.PAUSED, at=t_pause)
    assert session.status == TimelapseStatus.PAUSED
    assert session.paused_at == t_pause

    # Transition to CAPTURING (resumed)
    t_resume = datetime(2026, 9, 8, 12, 10, 0, tzinfo=timezone.utc)
    session.transition_to(TimelapseStatus.CAPTURING, at=t_resume)
    assert session.status == TimelapseStatus.CAPTURING
    assert session.resumed_at == t_resume

    # Transition to COMPLETED
    t_done = datetime(2026, 9, 8, 13, 0, 0, tzinfo=timezone.utc)
    session.transition_to(TimelapseStatus.COMPLETED, at=t_done)
    assert session.status == TimelapseStatus.COMPLETED
    assert session.completed_at == t_done


def test_pause_duration_tracking():
    t_start = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 9, 8, 12, 5, 30, tzinfo=timezone.utc)

    pause = TimelapsePause(session_id="session-1", started_at=t_start)
    duration = pause.close(t_end)

    assert duration == 330.0
    assert pause.duration_seconds == 330.0
    assert pause.ended_at == t_end


def test_storage_directory_hierarchy(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    t0 = datetime(2026, 9, 8, 14, 30, 0, tzinfo=timezone.utc)
    session_id = "tl_printer_job_1700000000"

    session_dir = storage.resolve_session_dir("bambu-a1", session_id, t0)
    expected_path = tmp_path / "bambu-a1" / "2026" / "09" / "08" / session_id
    assert session_dir == expected_path

    # Ensure directories
    s_dir, f_dir = storage.ensure_session_dirs(session_dir)
    assert s_dir.is_dir()
    assert f_dir.is_dir()
    assert f_dir.name == "frames"


def test_storage_frame_saving_and_naming(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_1"

    # Save frames 1, 2, 3
    p1 = storage.save_frame(session_dir, 1, MINI_JPEG)
    p2 = storage.save_frame(session_dir, 2, MINI_JPEG)
    p3 = storage.save_frame(session_dir, 3, MINI_JPEG)

    assert p1.name == "000001.jpg"
    assert p2.name == "000002.jpg"
    assert p3.name == "000003.jpg"

    assert p1.is_file()
    assert p1.read_bytes() == MINI_JPEG

    # List frames
    frames = storage.list_frames(session_dir)
    assert len(frames) == 3
    assert [f.name for f in frames] == ["000001.jpg", "000002.jpg", "000003.jpg"]

    # Retrieve individual frame
    retrieved = storage.get_frame_path(session_dir, 2)
    assert retrieved == p2

    # Missing frame
    assert storage.get_frame_path(session_dir, 99) is None


def test_storage_atomic_manifest_update(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_manifest"
    session_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession.create(
        printer_id="bambu-a1",
        print_job_id="job_456",
        camera_type="tapo_rtsp",
        capture_interval_seconds=5.0,
        video_fps=30,
        storage_dir=str(session_dir),
    )
    session.frame_count = 42
    session.missed_frames = 2
    session.paused_seconds = 120.5

    manifest_path = storage.save_manifest(session, session_dir)
    assert manifest_path.is_file()

    # Verify JSON content
    loaded = storage.load_manifest(session_dir)
    assert loaded is not None
    assert loaded.session_id == session.id
    assert loaded.print_job_id == "job_456"
    assert loaded.printer_id == "bambu-a1"
    assert loaded.frame_count == 42
    assert loaded.missed_frames == 2
    assert loaded.paused_seconds == 120.5
    assert loaded.camera["type"] == "tapo_rtsp"
    assert loaded.status == "capturing"


def test_cleanup_successful_frames(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_cleanup"

    storage.save_frame(session_dir, 1, MINI_JPEG)
    storage.save_frame(session_dir, 2, MINI_JPEG)
    assert len(storage.list_frames(session_dir)) == 2

    # Put a fake video and manifest
    video_path = storage.get_video_path(session_dir)
    video_path.write_bytes(b"fake mp4 video")
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text('{"status": "completed"}')

    # Cleanup frames
    deleted = storage.cleanup_successful_frames(session_dir)
    assert deleted == 2
    assert len(storage.list_frames(session_dir)) == 0

    # Ensure video and manifest remain intact
    assert video_path.is_file()
    assert manifest_path.is_file()
