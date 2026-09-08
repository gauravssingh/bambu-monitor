"""Unit tests for TelemetryOverlayBurner."""

import io
from pathlib import Path
from PIL import Image
import pytest

from bambu_monitor.timelapse.models import TimelapseSession, TimelapseStatus
from bambu_monitor.timelapse.overlay import TelemetryOverlayBurner
from bambu_monitor.timelapse.renderer import TimelapseRenderer
from bambu_monitor.timelapse.storage import TimelapseStorage


def test_burn_hud_nominal():
    img = Image.new("RGB", (640, 360), color=(30, 30, 30))
    telem = {
        "layer": 4,
        "total_layers": 20,
        "progress": 20.0,
        "nozzle_temp": 220.0,
        "nozzle_target": 220.0,
        "bed_temp": 60.0,
        "bed_target": 60.0,
        "speed_percent": 100,
    }
    overlaid = TelemetryOverlayBurner.burn_hud(img, telem, printer_label="A1 Mini")
    assert overlaid.size == (640, 360)
    assert overlaid.mode == "RGB"


def test_burn_hud_thermal_drop_alert():
    img = Image.new("RGB", (1280, 720), color=(20, 20, 20))
    telem = {
        "layer": 10,
        "total_layers": 20,
        "progress": 50.0,
        "nozzle_temp": 180.0,  # 40 deg below target
        "nozzle_target": 220.0,
        "bed_temp": 60.0,
        "bed_target": 60.0,
        "anomalies": ["anom-1"],
    }
    overlaid = TelemetryOverlayBurner.burn_hud(img, telem, printer_label="A1 Mini")
    assert overlaid.size == (1280, 720)


def test_burn_hud_to_bytes():
    img = Image.new("RGB", (320, 240), color=(40, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    orig_bytes = buf.getvalue()

    telem = {"nozzle_temp": 215.0, "nozzle_target": 220.0, "layer": 1}
    out_bytes = TelemetryOverlayBurner.burn_hud_to_bytes(orig_bytes, telem)
    assert out_bytes.startswith(b"\xff\xd8")
    assert len(out_bytes) > 0


@pytest.mark.asyncio
async def test_render_overlaid_frames_to_mp4(tmp_path: Path):
    storage = TimelapseStorage(base_dir=tmp_path)
    session_dir = tmp_path / "session_overlay_render"
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    session = TimelapseSession.create(
        printer_id="printer-1",
        print_job_id="job-overlay-test",
        capture_interval_seconds=1.0,
        video_fps=10,
        storage_dir=str(session_dir),
    )

    # Generate 5 small frames with overlays
    for i in range(1, 6):
        base_img = Image.new("RGB", (320, 240), color=(i * 30, i * 20, 80))
        telem = {
            "layer": i,
            "total_layers": 5,
            "progress": i * 20.0,
            "nozzle_temp": 220.0,
            "nozzle_target": 220.0,
        }
        overlaid = TelemetryOverlayBurner.burn_hud(base_img, telem)
        frame_path = frames_dir / f"{i:06d}.jpg"
        overlaid.save(frame_path, format="JPEG")

    renderer = TimelapseRenderer()
    video_path = await renderer.render(session=session, storage=storage, fps=10)

    assert video_path.is_file()
    assert video_path.stat().st_size > 0
    assert session.status == TimelapseStatus.COMPLETED
