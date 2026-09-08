"""Unit tests for TimelapseRenderer (FFmpeg invocation, atomic finalization, error handling)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
from bambu_monitor.timelapse.renderer import TimelapseRenderError, TimelapseRenderer
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
async def test_renderer_insufficient_frames(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_insufficient"
    session_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        storage_dir=str(session_dir),
    )

    renderer = TimelapseRenderer(min_frames=3)
    # 0 frames -> should fail
    with pytest.raises(TimelapseRenderError) as exc_info:
        await renderer.render(session, storage)

    assert "Insufficient frames" in str(exc_info.value)
    assert session.status == TimelapseStatus.FAILED


@pytest.mark.asyncio
async def test_renderer_success_mock_ffmpeg(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_mock_render"
    storage.save_frame(session_dir, 1, MINI_JPEG)
    storage.save_frame(session_dir, 2, MINI_JPEG)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        storage_dir=str(session_dir),
        video_fps=30,
    )

    renderer = TimelapseRenderer()

    # Mock FFmpeg process: simulate creating the temp file and exiting with code 0
    async def fake_ffmpeg(*args, **kwargs):
        out_file = Path(args[-1])
        out_file.write_bytes(b"fake mp4 video bytes")
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_ffmpeg):
        video_path = await renderer.render(session, storage)
        assert video_path.is_file()
        assert video_path.name == "timelapse.mp4"
        assert video_path.read_bytes() == b"fake mp4 video bytes"
        assert session.status == TimelapseStatus.COMPLETED
        assert session.video_path == str(video_path.resolve())

        # Verify no temp files left behind
        temp_files = list(session_dir.glob("*.tmp*"))
        assert len(temp_files) == 0


@pytest.mark.asyncio
async def test_renderer_failure_cleans_up_temp_file(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_fail"
    storage.save_frame(session_dir, 1, MINI_JPEG)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        storage_dir=str(session_dir),
    )

    renderer = TimelapseRenderer()

    async def fake_failing_ffmpeg(*args, **kwargs):
        out_file = Path(args[-1])
        out_file.write_bytes(b"corrupted partial video")
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"Encoder error: bitrate too high")
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_failing_ffmpeg):
        with pytest.raises(TimelapseRenderError) as exc_info:
            await renderer.render(session, storage)

        assert "FFmpeg rendering failed" in str(exc_info.value)
        assert session.status == TimelapseStatus.FAILED

        # Verify temp file was cleaned up and never left around as partial video
        final_video = storage.get_video_path(session_dir)
        assert not final_video.exists()
        temp_files = list(session_dir.glob("*.tmp*"))
        assert len(temp_files) == 0


@pytest.mark.asyncio
async def test_renderer_with_sequence_gaps(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_gaps"

    # Frame 1 and 3 present, frame 2 was missed
    storage.save_frame(session_dir, 1, MINI_JPEG)
    storage.save_frame(session_dir, 3, MINI_JPEG)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-1",
        storage_dir=str(session_dir),
    )

    renderer = TimelapseRenderer()

    recorded_args = []

    async def fake_ffmpeg(*args, **kwargs):
        recorded_args.extend(args)
        out_file = Path(args[-1])
        out_file.write_bytes(b"valid video")
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_ffmpeg):
        video_path = await renderer.render(session, storage)
        assert video_path.is_file()
        assert session.status == TimelapseStatus.COMPLETED

        # Check that -start_number was set to 1 for the re-sequenced frames
        assert "-start_number" in recorded_args
        assert "1" in recorded_args
