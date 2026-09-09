"""Unit tests for configuration loading and environment interpolation."""

from pathlib import Path
from bambu_monitor.config import Settings, _interpolate_env_vars, load_config


def test_interpolate_env_vars(monkeypatch):
    monkeypatch.setenv("TEST_BAMBU_HOST", "192.168.1.100")
    monkeypatch.setenv("TEST_PORT", "9999")

    template = "host: ${TEST_BAMBU_HOST}\nport: ${TEST_PORT}\nsecret: ${UNSET_SECRET:fallback_value}"
    result = _interpolate_env_vars(template)

    assert "host: 192.168.1.100" in result
    assert "port: 9999" in result
    assert "secret: fallback_value" in result


def test_default_config():
    settings = Settings()
    assert settings.application.name == "bambu-monitor"
    assert settings.database.journal_mode == "WAL"
    assert settings.database.flush_interval_seconds == 5.0
    assert len(settings.printers) >= 1
    assert settings.printers[0].id == "bambu-a1"
    assert settings.printers[0].port == 8883
    assert settings.printers[0].tls_verify is False
    assert settings.detection.stall.enabled is True


def test_yaml_config_loading(tmp_path: Path):
    config_file = tmp_path / "custom_config.yaml"
    config_file.write_text(
        """
application:
  name: test-monitor
  environment: test
database:
  path: ./custom/test.db
  flush_interval_seconds: 2.5
printers:
  - id: printer-1
    model: A1
    host: 10.0.0.1
    serial_number: SN001
  - id: printer-2
    model: A1 Mini
    host: 10.0.0.2
    serial_number: SN002
detection:
  stall:
    min_check_seconds: 600.0
""",
        encoding="utf-8",
    )

    settings = load_config(config_file)
    assert settings.application.name == "test-monitor"
    assert settings.database.path == "./custom/test.db"
    assert settings.database.flush_interval_seconds == 2.5
    assert len(settings.printers) == 2
    assert settings.printers[0].id == "printer-1"
    assert settings.printers[1].id == "printer-2"
    assert settings.detection.stall.min_check_seconds == 600.0


def test_per_printer_timelapse_overrides_only_explicit_fields(tmp_path: Path):
    """A per-printer `timelapse:` section containing only some keys must
    inherit the remaining values from the global timelapse config instead of
    silently replacing them with model defaults."""
    from bambu_monitor.config import PrinterConfig, Settings

    settings = Settings(
        printers=[
            PrinterConfig(
                id="p1",
                serial_number="SNMERGE1",
                host="10.0.0.1",
                timelapse={"enabled": False, "video": {"fps": 60}},
            )
        ]
    )
    settings.timelapse.video.fps = 24
    settings.timelapse.video.quality = 20
    settings.timelapse.capture.interval_seconds = 3.0

    cfg = settings.get_timelapse_config("p1")
    assert cfg.enabled is False                 # explicitly overridden
    assert cfg.video.fps == 60                  # explicitly overridden
    assert cfg.video.quality == 20              # inherited from global
    assert cfg.capture.interval_seconds == 3.0  # inherited from global


def test_printer_without_timelapse_section_inherits_global():
    from bambu_monitor.config import PrinterConfig, Settings

    settings = Settings(
        printers=[PrinterConfig(id="p2", serial_number="SNMERGE2", host="10.0.0.2")]
    )
    settings.timelapse.video.fps = 24
    cfg = settings.get_timelapse_config("p2")
    assert cfg.video.fps == 24
    assert cfg.enabled is True


def test_per_printer_section_without_enabled_key_inherits_global_enabled():
    """A printer `timelapse:` section that sets some field but not `enabled`
    must inherit the global `enabled`, not silently flip it via a model
    default (regression: cfg.enabled = p_tl.enabled was unconditional)."""
    from bambu_monitor.config import PrinterConfig, Settings

    settings = Settings(
        printers=[
            PrinterConfig(
                id="p3",
                serial_number="SNMERGE3",
                host="10.0.0.3",
                timelapse={"video": {"fps": 60}},
            )
        ]
    )
    settings.timelapse.enabled = False

    cfg = settings.get_timelapse_config("p3")
    assert cfg.enabled is False  # inherited from global, not the field default (True)
    assert cfg.video.fps == 60   # explicitly overridden


def test_legacy_timelapse_camera_url_key_still_populates_rtsp_url(tmp_path: Path):
    """CameraConfig replaced the old TimelapseCameraConfig for
    `timelapse.camera`; existing config.yaml files using the legacy `url:`
    key (this repo's own config.yaml included) must keep working unchanged."""
    from bambu_monitor.config import load_config

    config_file = tmp_path / "legacy_url_key.yaml"
    config_file.write_text(
        """
timelapse:
  camera:
    type: tapo_rtsp
    stream: stream1
    url: rtsp://user:pass@192.168.1.55:554/stream1
""",
        encoding="utf-8",
    )
    settings = load_config(config_file)
    assert settings.timelapse.camera.rtsp_url == "rtsp://user:pass@192.168.1.55:554/stream1"


def test_timelapse_camera_fallback_inherits_full_printer_camera_config():
    """When no dedicated timelapse camera URL is configured, timelapse must
    inherit the printer's ENTIRE camera config (ffmpeg/timeout/probe
    tuning included) rather than just the URL/type/stream fields — using
    the same camera for live snapshots and timelapse should behave
    identically in both places."""
    from bambu_monitor.camera import CameraConfig
    from bambu_monitor.config import PrinterConfig, Settings

    settings = Settings(
        printers=[
            PrinterConfig(
                id="p4",
                serial_number="SNFALLBACK4",
                host="10.0.0.4",
                camera=CameraConfig(
                    rtsp_url="rtsp://cam/stream1",
                    timeout_seconds=9.5,
                    ffmpeg_bin="/opt/custom/ffmpeg",
                ),
            )
        ]
    )
    # No timelapse.camera.url configured at all (default empty).
    cfg = settings.get_timelapse_config("p4")
    assert cfg.camera.rtsp_url == "rtsp://cam/stream1"
    assert cfg.camera.timeout_seconds == 9.5
    assert cfg.camera.ffmpeg_bin == "/opt/custom/ffmpeg"


def test_dedicated_timelapse_camera_url_is_not_overridden_by_printer_fallback():
    """A dedicated timelapse.camera.rtsp_url must win outright — the printer
    camera fallback only applies when no timelapse-specific URL is set."""
    from bambu_monitor.camera import CameraConfig
    from bambu_monitor.config import PrinterConfig, Settings, TimelapseConfig

    settings = Settings(
        printers=[
            PrinterConfig(
                id="p5",
                serial_number="SNFALLBACK5",
                host="10.0.0.5",
                camera=CameraConfig(rtsp_url="rtsp://printer-cam/stream1"),
            )
        ],
        timelapse=TimelapseConfig(camera=CameraConfig(rtsp_url="rtsp://dedicated-timelapse-cam/stream1")),
    )
    cfg = settings.get_timelapse_config("p5")
    assert cfg.camera.rtsp_url == "rtsp://dedicated-timelapse-cam/stream1"


def test_no_camera_configured_anywhere_leaves_rtsp_url_empty():
    from bambu_monitor.config import PrinterConfig, Settings

    settings = Settings(printers=[PrinterConfig(id="p6", serial_number="SNFALLBACK6", host="10.0.0.6")])
    cfg = settings.get_timelapse_config("p6")
    assert cfg.camera.rtsp_url == ""
