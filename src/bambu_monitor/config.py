"""Configuration management for Bambu Monitor."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, List, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bambu_monitor.camera.config import CameraConfig

# Load environment variables from .env file if present
load_dotenv()


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
    public_base_url: str = "http://localhost:8000"
    api_token: Optional[str] = None
    allow_unauthenticated_loopback: bool = True


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
    camera: Optional[CameraConfig] = Field(
        default=None,
        description="Optional RTSP camera configuration for visual monitoring",
    )
    timelapse: Optional[TimelapseConfig] = Field(
        default=None,
        description="Optional per-printer timelapse configuration overriding global settings",
    )

    @field_validator("access_code", mode="before")
    @classmethod
    def _coerce_access_code(cls, v: Any) -> str:
        return "" if v is None else str(v)


class DeliveryConfig(BaseModel):
    # Safe by default: a config-less install (no EVENT_SECRET, no config.yaml)
    # must boot cleanly. Enabling delivery requires an explicit opt-in plus a
    # secret (see api/app.py's startup check).
    enabled: bool = False
    endpoint: str = "http://localhost:8644/webhooks/bambu-printer"
    secret: Optional[str] = None
    timeout_seconds: int = 10
    retry_attempts: int = 5
    initial_backoff_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 300.0
    poll_interval_seconds: float = 1.0
    filter_events: Optional[List[str]] = None


class EventsConfig(BaseModel):
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


class StallDetectionConfig(BaseModel):
    enabled: bool = True
    min_check_seconds: float = 300.0
    adaptive_factor: float = 1.5


class DetectionConfig(BaseModel):
    stall: StallDetectionConfig = Field(default_factory=StallDetectionConfig)


class TimelapseCameraConfig(BaseModel):
    type: str = "tapo_rtsp"
    url: str = Field(default="", description="RTSP URL for timelapse camera")
    stream: str = Field(default="stream1", description="Stream profile, e.g. stream1 HD")

    @field_validator("url", mode="before")
    @classmethod
    def _coerce_url(cls, v: object) -> str:
        return "" if v is None else str(v).strip()


class TimelapseCaptureConfig(BaseModel):
    interval_seconds: float = Field(default=5.0, ge=0.01, description="Snapshot interval in seconds")
    mode: str = Field(default="interval", description="Capture mode: interval (V1), layer or hybrid (future)")


class TimelapseOverlayConfig(BaseModel):
    enabled: bool = Field(default=False, description="Whether to burn telemetry HUD directly into video frames")
    show_temperatures: bool = True
    show_progress: bool = True
    show_alerts: bool = True


class TimelapseVideoConfig(BaseModel):
    fps: int = Field(default=30, ge=1, le=120, description="Timelapse output video framerate")
    codec: str = Field(default="libx264", description="FFmpeg video codec")
    quality: int = Field(default=18, ge=0, le=51, description="FFmpeg Constant Rate Factor (CRF) quality")
    pixel_format: str = Field(default="yuv420p", description="Pixel format for maximum playback compatibility")
    max_concurrent: int = Field(default=1, ge=1, le=8, description="Maximum concurrent video render jobs")


class TimelapseRetentionConfig(BaseModel):
    successful_frames: str = Field(default="delete_after_video", description="Retention for successful print frames: delete_after_video or retain")
    failed_frames: str = Field(default="retain", description="Retention for failed print frames")
    videos_days: int = Field(default=365, ge=1, description="Days to retain completed video files")


class TimelapseConfig(BaseModel):
    enabled: bool = True
    storage_dir: str = Field(default="./data/timelapses", description="Root filesystem storage directory for timelapses")
    camera: TimelapseCameraConfig = Field(default_factory=TimelapseCameraConfig)
    capture: TimelapseCaptureConfig = Field(default_factory=TimelapseCaptureConfig)
    video: TimelapseVideoConfig = Field(default_factory=TimelapseVideoConfig)
    overlay: TimelapseOverlayConfig = Field(default_factory=TimelapseOverlayConfig)
    retention: TimelapseRetentionConfig = Field(default_factory=TimelapseRetentionConfig)


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
    timelapse: TimelapseConfig = Field(default_factory=TimelapseConfig)

    def get_timelapse_config(self, printer_id: str) -> TimelapseConfig:
        """Resolve effective timelapse configuration for a given printer.

        Only fields the user explicitly set in the per-printer ``timelapse:``
        section override the global defaults (detected via ``model_fields_set``).
        Nested sub-models are always constructed by Pydantic and therefore always
        truthy, so truthiness checks would wrongly discard global settings.
        """
        printer_cfg = next((p for p in self.printers if p.id == printer_id), None)
        cfg = self.timelapse.model_copy(deep=True)
        if printer_cfg and printer_cfg.timelapse:
            p_tl = printer_cfg.timelapse
            if "enabled" in p_tl.model_fields_set:
                cfg.enabled = p_tl.enabled
            if "storage_dir" in p_tl.model_fields_set:
                cfg.storage_dir = p_tl.storage_dir

            # Field-wise merge of explicitly-set nested keys
            for section in ("camera", "capture", "video", "overlay", "retention"):
                p_section: Optional[BaseModel] = getattr(p_tl, section, None)
                if p_section is None:
                    continue
                cfg_section: BaseModel = getattr(cfg, section)
                for field_name in p_section.model_fields_set:
                    setattr(cfg_section, field_name, getattr(p_section, field_name))

        if not cfg.camera.url and printer_cfg and printer_cfg.camera and printer_cfg.camera.rtsp_url:
            cfg.camera.url = printer_cfg.camera.rtsp_url
            if printer_cfg.camera.stream:
                cfg.camera.stream = printer_cfg.camera.stream
            if printer_cfg.camera.type:
                cfg.camera.type = printer_cfg.camera.type
        return cfg

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
    """Load configuration from the specified path or standard search locations.

    When loaded via default search locations, relative database paths are anchored
    to the configuration directory rather than arbitrary CWD.
    """
    if config_path:
        return Settings.from_yaml(config_path)

    candidates: List[Path] = []
    if os.environ.get("BAMBU_CONFIG_PATH"):
        candidates.append(Path(os.environ["BAMBU_CONFIG_PATH"]))

    for default_file in ("config.yaml", "config.yml"):
        candidates.append(Path(default_file))

    # Check repository/project root relative to this module
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(project_root / "config.yaml")
    candidates.append(project_root / "config.yml")
    candidates.append(Path.home() / ".config" / "bambu-monitor" / "config.yaml")

    found_path = next((c for c in candidates if c.is_file()), None)
    if found_path:
        settings = Settings.from_yaml(found_path)
        base_dir = found_path.resolve().parent
        db_path = Path(settings.database.path)
        if not db_path.is_absolute():
            settings.database.path = str((base_dir / db_path).resolve())
        storage_path = Path(settings.timelapse.storage_dir)
        if not storage_path.is_absolute():
            settings.timelapse.storage_dir = str((base_dir / storage_path).resolve())
        return settings

    return Settings()
