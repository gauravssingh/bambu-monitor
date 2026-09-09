"""Unit tests for RTSP Camera monitoring, CameraClient, security, and exception handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from bambu_monitor.camera import (
    CameraCaptureError,
    CameraClient,
    CameraConfig,
    CameraConfigError,
    CameraConnectionError,
    CameraError,
    CameraRegistry,
    CameraTimeoutError,
    sanitize_rtsp_url,
)

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"


# 1. Security & Sanitization
def test_sanitize_rtsp_url():
    raw = "rtsp://admin:supersecret123@192.168.1.100:554/stream1"
    sanitized = sanitize_rtsp_url(raw)
    assert sanitized == "rtsp://admin:***@192.168.1.100:554/stream1"
    assert "supersecret123" not in sanitized

    # Empty user
    raw_no_user = "rtsp://:secretpass@10.0.0.5:554/stream2"
    assert sanitize_rtsp_url(raw_no_user) == "rtsp://:***@10.0.0.5:554/stream2"

    # No credentials
    raw_no_auth = "rtsp://192.168.1.50:554/live"
    assert sanitize_rtsp_url(raw_no_auth) == "rtsp://192.168.1.50:554/live"

    # Multiline FFmpeg log
    log = (
        "Opening 'rtsp://user:pass99@camera.local:554/stream1' for reading\n"
        "Server returned 401 Unauthorized"
    )
    sanitized_log = sanitize_rtsp_url(log)
    assert "user:***@" in sanitized_log
    assert "pass99" not in sanitized_log

    # Password containing '/' (previously truncated netloc parsing and leaked)
    raw_slash_pw = "rtsp://admin:pa/ss@192.168.1.100:554/stream1"
    sanitized_slash = sanitize_rtsp_url(raw_slash_pw)
    assert sanitized_slash == "rtsp://admin:***@192.168.1.100:554/stream1"
    assert "pa/ss" not in sanitized_slash

    # Password containing ':' (previously partially leaked the prefix)
    raw_colon_pw = "rtsp://admin:pa:ss@192.168.1.100:554/stream1"
    sanitized_colon = sanitize_rtsp_url(raw_colon_pw)
    assert sanitized_colon == "rtsp://admin:***@192.168.1.100:554/stream1"
    assert "pa:ss" not in sanitized_colon

    # Password containing '@'
    raw_at_pw = "rtsp://admin:p@ss@192.168.1.100:554/stream1"
    sanitized_at = sanitize_rtsp_url(raw_at_pw)
    assert sanitized_at == "rtsp://admin:***@192.168.1.100:554/stream1"
    assert "p@ss" not in sanitized_at


def test_camera_exceptions_sanitization():
    exc = CameraError("Error connecting to rtsp://camuser:mypassword@192.168.1.200:554/stream1")
    assert "mypassword" not in str(exc)
    assert "camuser:***@" in str(exc)

    capture_exc = CameraCaptureError("FFmpeg failed: rtsp://admin:topsecret@10.0.0.1:554/stream1")
    assert "topsecret" not in str(capture_exc)
    assert "admin:***@" in str(capture_exc)


# 2. Configuration Model
def test_camera_config():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:pass@192.168.1.10:554/stream1",
        timeout_seconds=6.0,
        probe_size_bytes=2097152,
        analyze_duration_us=2000000,
    )
    assert cfg.sanitized_rtsp_url == "rtsp://admin:***@192.168.1.10:554/stream1"
    assert cfg.timeout_seconds == 6.0
    assert cfg.probe_size_bytes == 2097152
    assert cfg.analyze_duration_us == 2000000


# 3. CameraClient.capture() Success
@pytest.mark.asyncio
async def test_snapshot_success():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
        timeout_seconds=4.0,
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (FAKE_JPEG, b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await client.capture()
        assert result == FAKE_JPEG
        assert mock_exec.called
        args = mock_exec.call_args[0]
        assert args[0] == "ffmpeg"
        assert "-rtsp_transport" in args
        assert "tcp" in args
        assert "-f" in args
        assert "image2pipe" in args


# 4. CameraClient.capture() FFmpeg Failure & Sanitization
@pytest.mark.asyncio
async def test_snapshot_ffmpeg_failure():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (
        b"",
        b"Connection refused to rtsp://admin:secret@192.168.1.10:554/stream1\n",
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CameraConnectionError) as exc_info:
            await client.capture()

        err_msg = str(exc_info.value)
        assert "secret" not in err_msg
        assert "admin:***@" in err_msg


# 5. CameraClient.capture() Timeout with Guaranteed Process Kill
@pytest.mark.asyncio
async def test_snapshot_timeout_kills_process():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
        timeout_seconds=1.0,
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    mock_proc = AsyncMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.communicate.side_effect = asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CameraTimeoutError) as exc_info:
            await client.capture()

        assert "timed out after 1.0s" in str(exc_info.value)
        # Verify guaranteed process cleanup
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()


# 6. CameraClient.capture() Empty Output & Invalid Output
@pytest.mark.asyncio
async def test_snapshot_empty_stdout():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CameraCaptureError) as exc_info:
            await client.capture()
        assert "empty image" in str(exc_info.value)


@pytest.mark.asyncio
async def test_snapshot_invalid_magic_bytes():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"corrupted_non_jpeg_data", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CameraCaptureError) as exc_info:
            await client.capture()
        assert "not a valid JPEG" in str(exc_info.value)


# 7. Disabled Camera / Missing URL
@pytest.mark.asyncio
async def test_snapshot_disabled_camera():
    cfg = CameraConfig(
        enabled=False,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
    )
    client = CameraClient(config=cfg, printer_id="test-a1")
    with pytest.raises(CameraConfigError) as exc_info:
        await client.capture()
    assert "Camera is disabled" in str(exc_info.value)


@pytest.mark.asyncio
async def test_snapshot_missing_url():
    cfg = CameraConfig(enabled=True, rtsp_url="")
    client = CameraClient(config=cfg, printer_id="test-a1")
    with pytest.raises(CameraConfigError) as exc_info:
        await client.capture()
    assert "RTSP URL is not configured" in str(exc_info.value)


# 8. test_connection() Diagnostics
@pytest.mark.asyncio
async def test_test_connection_success():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    # Mock snapshot and ffprobe
    mock_ffmpeg_proc = AsyncMock()
    mock_ffmpeg_proc.returncode = 0
    mock_ffmpeg_proc.communicate.return_value = (FAKE_JPEG, b"")

    mock_ffprobe_proc = AsyncMock()
    mock_ffprobe_proc.returncode = 0
    probe_json = b'{"streams": [{"codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "15/1"}]}'
    mock_ffprobe_proc.communicate.return_value = (probe_json, b"")

    async def side_effect(*args, **kwargs):
        if args[0] == "ffmpeg":
            return mock_ffmpeg_proc
        return mock_ffprobe_proc

    with patch("asyncio.create_subprocess_exec", side_effect=side_effect):
        diag = await client.test_connection()
        assert diag["connected"] is True
        assert diag["image_size_bytes"] == len(FAKE_JPEG)
        assert diag["codec"] == "h264"
        assert diag["resolution"] == "1920x1080"
        assert diag["fps"] == "15.0 fps"
        assert "secret" not in diag["sanitized_url"]


@pytest.mark.asyncio
async def test_test_connection_failure():
    cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.10:554/stream1",
    )
    client = CameraClient(config=cfg, printer_id="test-a1")

    mock_ffmpeg_proc = AsyncMock()
    mock_ffmpeg_proc.returncode = 1
    mock_ffmpeg_proc.communicate.return_value = (b"", b"Connection refused to rtsp://admin:secret@192.168.1.10:554/stream1")

    with patch("asyncio.create_subprocess_exec", return_value=mock_ffmpeg_proc):
        diag = await client.test_connection()
        assert diag["connected"] is False
        assert "secret" not in diag["error"]
        assert "admin:***@" in diag["error"]


# 9. CameraRegistry
def test_camera_registry():
    reg = CameraRegistry()
    cfg = CameraConfig(enabled=True, rtsp_url="rtsp://admin:pass@10.0.0.1:554/stream1")
    client = CameraClient(config=cfg, printer_id="printer-1")

    reg.register("printer-1", client)
    assert reg.get("printer-1") is client
    assert reg.get("printer-2") is None

    all_cams = reg.list_all()
    assert "printer-1" in all_cams

    removed = reg.remove("printer-1")
    assert removed is client
    assert reg.get("printer-1") is None


# 10. TapoRTSPCamera & Protocol Conformance
def test_tapo_and_generic_camera_protocol():
    from bambu_monitor.camera import (
        CameraClient,
        CameraClientProtocol,
        TapoRTSPCamera,
        create_camera_client,
    )

    tapo_cfg = CameraConfig(type="tapo_rtsp", rtsp_url="rtsp://admin:pass@192.168.1.50:554/stream1")
    tapo = TapoRTSPCamera(config=tapo_cfg, printer_id="printer-1")
    assert isinstance(tapo, CameraClientProtocol)
    assert tapo.camera_type == "tapo_rtsp"

    generic_cfg = CameraConfig(type="generic_rtsp", rtsp_url="rtsp://admin:pass@192.168.1.60:554/live")
    generic = CameraClient(config=generic_cfg, printer_id="printer-2")
    assert isinstance(generic, CameraClientProtocol)
    assert generic.camera_type == "generic_rtsp"

    created_tapo = create_camera_client(tapo_cfg, printer_id="printer-1")
    assert isinstance(created_tapo, TapoRTSPCamera)

    created_generic = create_camera_client(generic_cfg, printer_id="printer-2")
    assert type(created_generic) is CameraClient


@pytest.mark.asyncio
async def test_tapo_camera_stream_selection():
    from bambu_monitor.camera import TapoRTSPCamera

    cfg = CameraConfig(
        type="tapo_rtsp",
        rtsp_url="rtsp://admin:pass@192.168.1.50:554/stream1",
        substream_url="rtsp://admin:pass@192.168.1.50:554/stream2",
        stream="stream2",
    )
    cam = TapoRTSPCamera(config=cfg, printer_id="tapo-printer")
    assert cam._get_active_stream_url() == "rtsp://admin:pass@192.168.1.50:554/stream2"

    cfg_main = CameraConfig(
        type="tapo_rtsp",
        rtsp_url="rtsp://admin:pass@192.168.1.50:554/stream1",
        stream="stream1",
    )
    cam_main = TapoRTSPCamera(config=cfg_main, printer_id="tapo-printer")
    assert cam_main._get_active_stream_url() == "rtsp://admin:pass@192.168.1.50:554/stream1"


@pytest.mark.asyncio
async def test_camera_connect_and_health_success():
    from bambu_monitor.camera import CameraHealth, TapoRTSPCamera

    cfg = CameraConfig(
        type="tapo_rtsp",
        rtsp_url="rtsp://admin:pass@192.168.1.50:554/stream1",
    )
    cam = TapoRTSPCamera(config=cfg, printer_id="tapo-printer")

    mock_ffmpeg_proc = AsyncMock()
    mock_ffmpeg_proc.returncode = 0
    mock_ffmpeg_proc.communicate.return_value = (FAKE_JPEG, b"")

    mock_ffprobe_proc = AsyncMock()
    mock_ffprobe_proc.returncode = 0
    probe_json = b'{"streams": [{"codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30/1"}]}'
    mock_ffprobe_proc.communicate.return_value = (probe_json, b"")

    async def side_effect(*args, **kwargs):
        if args[0] == "ffmpeg":
            return mock_ffmpeg_proc
        return mock_ffprobe_proc

    with patch("asyncio.create_subprocess_exec", side_effect=side_effect):
        # connect() should succeed without exception
        await cam.connect()

        # health() should return CameraHealth instance
        health = await cam.health()
        assert isinstance(health, CameraHealth)
        assert health.connected is True
        assert health.resolution == "1920x1080"
        assert health.codec == "h264"
        assert health.fps == "30.0 fps"
        assert health.camera_type == "tapo_rtsp"
        assert health.image_size_bytes == len(FAKE_JPEG)
        assert health.latency_ms is not None

        # close() should run cleanly
        await cam.close()


@pytest.mark.asyncio
async def test_camera_connect_failure():
    from bambu_monitor.camera import CameraConnectionError, TapoRTSPCamera

    cfg = CameraConfig(
        type="tapo_rtsp",
        rtsp_url="rtsp://admin:pass@192.168.1.50:554/stream1",
    )
    cam = TapoRTSPCamera(config=cfg, printer_id="tapo-printer")

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"Connection refused")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CameraConnectionError):
            await cam.connect()

