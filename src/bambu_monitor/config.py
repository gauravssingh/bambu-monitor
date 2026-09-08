"""Configuration management for Bambu Monitor."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, List

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _interpolate_env_vars(raw: str) -> str:
    """Replace ${VAR_NAME} or ${VAR_NAME:default} with environment variable values."""
    pattern = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var_name, default_val)

    return pattern.sub(replacer, raw)


class ApplicationConfig(BaseModel):
    name: str = "bambu-monitor"
    environment: str = "development"
    log_level: str = "INFO"
    auto_connect_mqtt: bool = True
    enable_discovery: bool = True


class DatabaseConfig(BaseModel):
    path: str = "./data/bambu.db"
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"
    flush_interval_seconds: float = 5.0


class PrinterConfig(BaseModel):
    id: str = "bambu-a1"
    model: str = "A1"
    host: str = "127.0.0.1"
    serial_number: str = "01P00A123456789"
    access_code: str = ""
    username: str = "bblp"
    port: int = 8883
    tls: bool = True
    # tls_verify is strictly scoped to local Bambu printer communication:
    # Bambu Lab printers in LAN Mode generate an untrusted, self-signed local X.509 certificate
    # for their embedded MQTT broker on port 8883. Disabling verification enables encrypted local
    # transport without requiring a custom CA root. This setting MUST NOT be reused for external
    # outbound integrations (such as Hermes webhooks or cloud APIs).
    tls_verify: bool = Field(
        default=False,
        description="Bambu local MQTT self-signed certificate verification bypass (local printer only)",
    )

    @field_validator("access_code", mode="before")
    @classmethod
    def _coerce_access_code(cls, v: Any) -> str:
        return "" if v is None else str(v)


class DeliveryConfig(BaseModel):
    enabled: bool = True
    endpoint: str = "http://localhost:8080/events"
    timeout_seconds: int = 10
    retry_attempts: int = 5
    initial_backoff_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 300.0


class EventsConfig(BaseModel):
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


class StallDetectionConfig(BaseModel):
    enabled: bool = True
    min_check_seconds: float = 300.0
    adaptive_factor: float = 1.5


class DetectionConfig(BaseModel):
    stall: StallDetectionConfig = Field(default_factory=StallDetectionConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    printers: List[PrinterConfig] = Field(
        default_factory=lambda: [PrinterConfig()]
    )
    events: EventsConfig = Field(default_factory=EventsConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """Load settings from a YAML file with environment variable expansion."""
        file_path = Path(path)
        if not file_path.exists():
            return cls()

        content = file_path.read_text(encoding="utf-8")
        interpolated = _interpolate_env_vars(content)
        data: dict[str, Any] = yaml.safe_load(interpolated) or {}
        return cls.model_validate(data)


def load_config(config_path: str | Path | None = None) -> Settings:
    """Load configuration from the specified path or default locations."""
    if config_path:
        return Settings.from_yaml(config_path)

    for default_file in ("config.yaml", "config.yml"):
        p = Path(default_file)
        if p.exists():
            return Settings.from_yaml(p)

    return Settings()
