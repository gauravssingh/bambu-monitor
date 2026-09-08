"""Domain models for telemetry patches and raw payload mapping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from bambu_monitor.domain.printer import PrinterState


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def map_gcode_state_to_printer_state(gcode_state: str | None) -> PrinterState | None:
    if not gcode_state:
        return None
    state_upper = gcode_state.upper()
    mapping = {
        "IDLE": PrinterState.IDLE,
        "PREPARE": PrinterState.PREPARING,
        "RUNNING": PrinterState.PRINTING,
        "PAUSE": PrinterState.PAUSED,
        "FINISH": PrinterState.COMPLETED,
        "FAILED": PrinterState.FAILED,
        "OFFLINE": PrinterState.OFFLINE,
    }
    return mapping.get(state_upper, PrinterState.UNKNOWN)


class TelemetryPatch(BaseModel):
    """Normalized partial telemetry patch.
    
    Crucial contract: Only fields explicitly provided are updated.
    Missing fields (None) must never overwrite or clear existing state.
    """
    printer_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    online: Optional[bool] = None
    state: Optional[PrinterState] = None
    gcode_state: Optional[str] = None
    progress: Optional[int] = None
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    remaining_seconds: Optional[int] = None
    nozzle_temperature: Optional[float] = None
    nozzle_target_temperature: Optional[float] = None
    bed_temperature: Optional[float] = None
    bed_target_temperature: Optional[float] = None
    chamber_temperature: Optional[float] = None
    subtask_name: Optional[str] = None
    error_code: Optional[int] = None

    @classmethod
    def from_raw(cls, printer_id: str, raw: Dict[str, Any], timestamp: Optional[datetime] = None) -> TelemetryPatch:
        """Parse raw telemetry dictionary (supporting canonical or Bambu field names)."""
        ts = timestamp
        if ts is None and "timestamp" in raw:
            raw_ts = raw["timestamp"]
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    ts = datetime.fromisoformat(raw_ts)
                except Exception:
                    pass
        if ts is None and isinstance(raw.get("print"), dict) and "timestamp" in raw["print"]:
            raw_ts = raw["print"]["timestamp"]
            if isinstance(raw_ts, datetime):
                ts = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    ts = datetime.fromisoformat(raw_ts)
                except Exception:
                    pass
        if ts is None:
            ts = utc_now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        
        # Extract from nested 'print' object if present (common in Bambu MQTT reports)
        payload = raw.get("print", raw)
        
        gcode_state = payload.get("gcode_state")
        derived_state = None
        if "state" in payload and payload["state"] is not None:
            if isinstance(payload["state"], PrinterState):
                derived_state = payload["state"]
            else:
                try:
                    derived_state = PrinterState(str(payload["state"]).lower())
                except ValueError:
                    derived_state = map_gcode_state_to_printer_state(str(payload["state"]))
        elif gcode_state:
            derived_state = map_gcode_state_to_printer_state(gcode_state)

        # Progress / layers
        progress = payload.get("progress")
        if progress is None and "mc_percent" in payload:
            progress = int(payload["mc_percent"])

        layer = payload.get("layer")
        if layer is None and "layer_num" in payload:
            layer = int(payload["layer_num"])

        total_layers = payload.get("total_layers")
        if total_layers is None and "total_layer_num" in payload:
            total_layers = int(payload["total_layer_num"])

        # Remaining seconds
        remaining_seconds = payload.get("remaining_seconds")
        if remaining_seconds is None and "mc_remaining_time" in payload:
            # Bambu reports mc_remaining_time in minutes; convert to seconds if integer
            val = payload["mc_remaining_time"]
            if val is not None:
                # If value is already large (>1000), it might already be seconds
                remaining_seconds = int(val) * 60 if int(val) < 10000 else int(val)

        # Temperatures
        nozzle_temp = payload.get("nozzle_temperature")
        if nozzle_temp is None and "nozzle_temper" in payload:
            nozzle_temp = float(payload["nozzle_temper"])

        nozzle_target = payload.get("nozzle_target_temperature")
        if nozzle_target is None and "nozzle_target_temper" in payload:
            nozzle_target = float(payload["nozzle_target_temper"])

        bed_temp = payload.get("bed_temperature")
        if bed_temp is None and "bed_temper" in payload:
            bed_temp = float(payload["bed_temper"])

        bed_target = payload.get("bed_target_temperature")
        if bed_target is None and "bed_target_temper" in payload:
            bed_target = float(payload["bed_target_temper"])

        chamber_temp = payload.get("chamber_temperature")
        if chamber_temp is None and "chamber_temper" in payload:
            chamber_temp = float(payload["chamber_temper"])

        # Subtask / filename
        subtask_name = payload.get("subtask_name")
        if subtask_name is None and "gcode_file" in payload:
            subtask_name = str(payload["gcode_file"])

        # Error codes
        error_code = payload.get("error_code")
        if error_code is None and "print_error" in payload:
            error_code = int(payload["print_error"])

        raw_online = payload.get("online")
        if isinstance(raw_online, bool):
            online = raw_online
        elif isinstance(raw_online, dict):
            # Bambu submodule status dictionary: presence proves printer is online
            online = True
        else:
            online = None

        return cls(
            printer_id=printer_id,
            timestamp=ts,
            online=online,
            state=derived_state,
            gcode_state=gcode_state,
            progress=progress,
            layer=layer,
            total_layers=total_layers,
            remaining_seconds=remaining_seconds,
            nozzle_temperature=nozzle_temp,
            nozzle_target_temperature=nozzle_target,
            bed_temperature=bed_temp,
            bed_target_temperature=bed_target,
            chamber_temperature=chamber_temp,
            subtask_name=subtask_name,
            error_code=error_code,
        )
