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
