"""Domain models for 3D Printers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PrinterState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class TemperatureInfo(BaseModel):
    nozzle: float = 0.0
    nozzle_target: float = 0.0
    bed: float = 0.0
    bed_target: float = 0.0
    chamber: Optional[float] = None


class PrintJobSnapshot(BaseModel):
    job_id: str
    filename: str
    status: str
    progress: int = 0
    layer: int = 0
    total_layers: int = 0
    remaining_seconds: Optional[int] = None
    started_at: Optional[datetime] = None


class CurrentPrinterState(BaseModel):
    printer_id: str
    model: str
    online: bool = False
    state: PrinterState = PrinterState.UNKNOWN
    print: Optional[PrintJobSnapshot] = None
    temperatures: TemperatureInfo = Field(default_factory=TemperatureInfo)
    speed_level: Optional[int] = None
    speed_magnitude: Optional[int] = None
    cooling_fan_speed: Optional[int] = None
    last_seen: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class Printer(BaseModel):
    """Printer identity entity. Does not expose credentials."""
    id: str
    model: str
    serial_number: str
    host: str
    online: bool = False
    last_seen: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)
