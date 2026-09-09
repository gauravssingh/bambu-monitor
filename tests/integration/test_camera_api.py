"""Integration tests for camera REST endpoints and CLI commands."""

from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
from httpx import AsyncClient

from bambu_monitor.camera import (
    CameraCaptureError,
    CameraClient,
    CameraConfig,
    CameraTimeoutError,
)
from bambu_monitor.cli.commands import cmd_camera_snap, cmd_camera_test
from bambu_monitor.config import PrinterConfig, Settings

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"


@pytest.mark.asyncio
async def test_snapshot_endpoint_success(async_client: AsyncClient):
    app = async_client._transport.app
    cam_cfg = CameraConfig(
        enabled=True,
        rtsp_url="rtsp://admin:secret@192.168.1.55:554/stream1",
    )
    cam_client = CameraClient(config=cam_cfg, printer_id="test-a1")
    cam_client.snapshot = AsyncMock(return_value=FAKE_JPEG)
    app.state.camera_registry.register("test-a1", cam_client)

    resp = await async_client.get("/api/v1/printers/test-a1/camera/snapshot")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == FAKE_JPEG


@pytest.mark.asyncio
async def test_snapshot_endpoint_missing_printer(async_client: AsyncClient):
    resp = await async_client.get("/api/v1/printers/non-existent-printer/camera/snapshot")
    assert resp.status_code == 404
    assert "Printer 'non-existent-printer' not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_snapshot_endpoint_no_camera_configured(async_client: AsyncClient):
    # test-a1-mini printer exists, but no camera is registered for it
    resp = await async_client.get("/api/v1/printers/test-a1-mini/camera/snapshot")
    assert resp.status_code == 404
    assert "No active camera configured" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_snapshot_endpoint_timeout(async_client: AsyncClient):
    app = async_client._transport.app
    cam_cfg = CameraConfig(enabled=True, rtsp_url="rtsp://admin:secret@10.0.0.1:554/stream1")
    cam_client = CameraClient(config=cam_cfg, printer_id="test-a1")
    cam_client.snapshot = AsyncMock(
        side_effect=CameraTimeoutError("Camera snapshot for 'test-a1' timed out after 5.0s")
    )
    app.state.camera_registry.register("test-a1", cam_client)

    resp = await async_client.get("/api/v1/printers/test-a1/camera/snapshot")
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_snapshot_endpoint_capture_error(async_client: AsyncClient):
    app = async_client._transport.app
    cam_cfg = CameraConfig(enabled=True, rtsp_url="rtsp://admin:secret@10.0.0.1:554/stream1")
    cam_client = CameraClient(config=cam_cfg, printer_id="test-a1")
    cam_client.snapshot = AsyncMock(
        side_effect=CameraCaptureError("Failed to connect to camera stream: Connection refused")
    )
    app.state.camera_registry.register("test-a1", cam_client)

    resp = await async_client.get("/api/v1/printers/test-a1/camera/snapshot")
    assert resp.status_code == 502
    assert "Connection refused" in resp.json()["detail"]


# CLI Commands Tests
@pytest.mark.asyncio
async def test_cli_camera_snap(tmp_path: Path, capsys):
    out_file = tmp_path / "test_cli_snap.jpg"
    settings = Settings(
        printers=[
            PrinterConfig(
                id="cam-printer",
                camera=CameraConfig(
                    enabled=True,
                    rtsp_url="rtsp://admin:pass@192.168.1.10:554/stream1",
                ),
            )
        ]
    )

    with patch.object(CameraClient, "snapshot", new_callable=AsyncMock) as mock_snap:
        mock_snap.return_value = FAKE_JPEG
        await cmd_camera_snap("cam-printer", output=str(out_file), settings=settings)

        captured = capsys.readouterr().out
        assert "Captured snapshot successfully" in captured
        assert out_file.exists()
        assert out_file.read_bytes() == FAKE_JPEG


@pytest.mark.asyncio
async def test_cli_camera_test(capsys):
    settings = Settings(
        printers=[
            PrinterConfig(
                id="cam-printer",
                camera=CameraConfig(
                    enabled=True,
                    rtsp_url="rtsp://admin:pass@192.168.1.10:554/stream1",
                ),
            )
        ]
    )

    fake_diag = {
        "connected": True,
        "snapshot_latency_ms": 412.5,
        "image_size_bytes": 120400,
        "codec": "h264",
        "resolution": "1920x1080",
        "fps": "15.0 fps",
    }

    with patch.object(CameraClient, "test_connection", new_callable=AsyncMock) as mock_test:
        mock_test.return_value = fake_diag
        await cmd_camera_test("cam-printer", settings=settings)

        captured = capsys.readouterr().out
        assert "Connection & Handshake: SUCCESS" in captured
        assert "412.5 ms" in captured
        assert "1920x1080" in captured
        assert "15.0 fps" in captured
