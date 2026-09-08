"""Unit tests for configuration loading and environment interpolation."""

import os
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
